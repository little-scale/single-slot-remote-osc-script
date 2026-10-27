"""One-cell Session ring, polled entirely on Live's control-surface thread."""
import errno
import json
import math
import socket

from _Framework.ControlSurface import ControlSurface
from . import config
from .osc import decode, encode


def integer(value):
    result = int(value)
    if isinstance(value, bool) or result != float(value):
        raise ValueError("Expected an integer")
    return result


def number(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Expected a finite number")
    return result


def boolean(value):
    if value not in (0, 1):
        raise ValueError("Expected 0 or 1")
    return bool(value)


class SingleSlotOSC(ControlSurface):
    def __init__(self, c_instance):
        super(SingleSlotOSC, self).__init__(c_instance)
        self._socket = None
        self._peer = None
        self._track = None
        self._scene = None
        self._x = self._y = 0
        self._layout = None
        self._ticks = 0
        self._last_state = None
        self._held_slot = None
        self._alive = True
        self._follow = False
        self._last_selection = None
        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._socket.setblocking(False)
            self._socket.bind((config.LISTEN_HOST, config.LISTEN_PORT))
        except OSError as error:
            if self._socket:
                self._socket.close()
            self._socket = None
            self.log_message("SingleSlotOSC bind failed: %s" % error)
            self.show_message("SingleSlotOSC: port %d unavailable; see Log.txt" % config.LISTEN_PORT)
        self._reconcile()
        if self._socket:
            self.show_message("SingleSlotOSC ready: UDP %d" % config.LISTEN_PORT)

    def _tracks(self):
        return tuple(self.song().visible_tracks)

    def _reconcile(self):
        tracks, scenes = self._tracks(), tuple(self.song().scenes)
        if tracks and scenes:
            self._x = tracks.index(self._track) if self._track in tracks else min(self._x, len(tracks) - 1)
            self._y = scenes.index(self._scene) if self._scene in scenes else min(self._y, len(scenes) - 1)
            self._track, self._scene = tracks[self._x], scenes[self._y]
            layout = (tracks, scenes, self._x, self._y)
            if layout != self._layout:
                self._release()
                self._layout = layout
                self._highlight()
        else:
            self._release()
            self._track = self._scene = None
            self._layout = None
            self._c_instance.set_session_highlight(-1, -1, -1, -1, False)

    def _highlight(self):
        self._c_instance.set_session_highlight(self._x, self._y, 1, 1, False)

    def _slot(self):
        if self._track is None or self._scene is None:
            raise ValueError("No visible Session slot")
        return self._track.clip_slots[self._y]

    def _clip(self):
        slot = self._slot()
        if not slot.has_clip:
            raise ValueError("Focused slot is empty")
        return slot.clip

    def _release(self):
        if self._held_slot is not None:
            try:
                self._held_slot.set_fire_button_state(False)
            except (RuntimeError, ReferenceError):
                pass
            self._held_slot = None

    def _select(self):
        slot = self._slot()
        view = self.song().view
        view.selected_track = self._track
        view.selected_scene = self._scene
        view.highlighted_clip_slot = slot
        if slot.has_clip:
            view.detail_clip = slot.clip
        self._last_selection = (self._track, self._scene)

    def _move_to(self, x, y):
        tracks, scenes = self._tracks(), tuple(self.song().scenes)
        if not tracks or not scenes:
            raise ValueError("No visible Session slot")
        self._release()
        self._x = max(0, min(x, len(tracks) - 1))
        self._y = max(0, min(y, len(scenes) - 1))
        self._track, self._scene = tracks[self._x], scenes[self._y]
        self._layout = None
        self._reconcile()
        self._select()

    def _send(self, address, args):
        if self._socket is not None and self._peer:
            try:
                self._socket.sendto(encode(address, args), self._peer)
            except (OSError, ValueError, OverflowError):
                pass

    def _state(self):
        state = dict(track=self._x if self._track else -1,
                     scene=self._y if self._scene else -1,
                     tracks=len(self._tracks()), scenes=len(self.song().scenes),
                     track_name=self._track.name if self._track else "",
                     has_clip=False, follow_selection=self._follow)
        if self._track is not None and self._scene is not None:
            slot = self._slot()
            state.update(has_clip=bool(slot.has_clip),
                         has_stop_button=bool(slot.has_stop_button),
                         is_group_slot=bool(slot.is_group_slot),
                         is_triggered=bool(slot.is_triggered),
                         can_be_armed=bool(self._track.can_be_armed))
            if self._track.can_be_armed:
                state['armed'] = bool(self._track.arm)
            if slot.has_clip:
                clip = slot.clip
                for key in ('name', 'is_playing', 'is_recording', 'is_audio_clip',
                            'muted', 'looping', 'loop_start', 'loop_end',
                            'start_marker', 'end_marker', 'launch_mode',
                            'launch_quantization', 'legato', 'color'):
                    state[key] = getattr(clip, key)
        return json.dumps(state, ensure_ascii=False, sort_keys=True)

    def _broadcast_state(self, force=False):
        state = self._state()
        if force or state != self._last_state:
            self._send('/focus/state', [state])
            self._last_state = state

    def _dispatch(self, address, args):
        # Explicit routing: no arbitrary setattr or method execution from the network.
        if not address.startswith('/focus/'):
            raise ValueError("Unknown OSC address")
        action = address[len('/focus/'):].lower()
        no_args = {'left', 'right', 'up', 'down', 'home', 'select', 'adopt_selection',
                   'show', 'fire', 'trigger', 'stop', 'toggle', 'delete', 'duplicate_loop',
                   'crop', 'state', 'ping', 'mute_toggle', 'loop_toggle', 'stop_scrub'}
        arities = {'move': (2,), 'goto': (2,), 'follow_selection': (1,),
                   'fire_button': (1,), 'record': (0, 1), 'create': (1,),
                   'load_audio': (1,), 'duplicate': (2,), 'has_stop_button': (1,),
                   'arm': (1,), 'name': (1,), 'color': (1,), 'mute': (1,),
                   'loop': (1,), 'loop_region': (2,), 'markers': (2,),
                   'pitch': (1,), 'detune': (1,), 'gain': (1,), 'warping': (1,),
                   'warp_mode': (1,), 'launch_mode': (1,), 'launch_quantization': (1,),
                   'legato': (1,), 'quantize': (2,), 'scrub': (1,)}
        allowed = (0,) if action in no_args else arities.get(action)
        if allowed is None:
            raise ValueError("Unknown action: " + action)
        if len(args) not in allowed:
            raise ValueError("Wrong argument count for " + action)
        if action in ('left', 'right', 'up', 'down', 'move', 'goto', 'home'):
            dx, dy = {'left': (-1, 0), 'right': (1, 0), 'up': (0, -1), 'down': (0, 1)}.get(action, (0, 0))
            if action == 'move':
                dx, dy = map(integer, args)
            x, y = (map(integer, args) if action == 'goto' else
                    (0, 0) if action == 'home' else (self._x + dx, self._y + dy))
            self._move_to(x, y)
        elif action in ('select', 'show'):
            self._select()
            if action == 'show':
                self.application().view.show_view('Session')
                self.application().view.show_view('Detail/Clip')
        elif action == 'adopt_selection':
            view = self.song().view
            tracks = self._tracks()
            if view.selected_track not in tracks:
                raise ValueError("Selected track has no visible Session slot")
            self._move_to(tracks.index(view.selected_track), tuple(self.song().scenes).index(view.selected_scene))
        elif action == 'follow_selection':
            self._follow = boolean(args[0])
            if self._follow:
                self._dispatch('/focus/adopt_selection', [])
        elif action in ('ping', 'state'):
            if action == 'ping':
                self._send('/focus/pong', ['SingleSlotOSC 1.0'])
        elif action in ('fire', 'trigger'):
            self._slot().fire()
        elif action == 'stop':
            self._release()
            self._slot().stop()
        elif action == 'fire_button':
            pressed = boolean(args[0])
            if pressed:
                self._release()
                self._held_slot = self._slot()
                self._held_slot.set_fire_button_state(True)
            else:
                self._release()
        elif action == 'toggle':
            clip = self._clip()
            self._slot().stop() if clip.is_playing or clip.is_triggered else self._slot().fire()
        elif action == 'record':
            slot = self._slot()
            if slot.has_clip or not self._track.can_be_armed or self._track.is_frozen:
                raise ValueError("Record needs an empty slot on an armable, unfrozen track")
            length = number(args[0]) if args else None
            if length is not None and length <= 0:
                raise ValueError("Record length must be positive beats")
            self._track.arm = True
            slot.fire(record_length=length) if length is not None else slot.fire()
        elif action == 'create':
            length = number(args[0])
            if self._slot().has_clip or not self._track.has_midi_input or length <= 0:
                raise ValueError("Create needs an empty MIDI slot and positive beat length")
            self._slot().create_clip(length)
        elif action == 'load_audio':
            if self._slot().has_clip or not self._track.has_audio_input:
                raise ValueError("Load needs an empty audio slot")
            self._slot().create_audio_clip(str(args[0]))
        elif action == 'delete':
            self._clip()
            self._release()
            self._slot().delete_clip()
        elif action == 'duplicate':
            self._clip()
            x, y = map(integer, args)
            tracks, scenes = self._tracks(), tuple(self.song().scenes)
            if not (0 <= x < len(tracks) and 0 <= y < len(scenes)):
                raise ValueError("Duplicate destination out of bounds")
            target = tracks[x].clip_slots[y]
            if target.has_clip or target == self._slot() or target.is_group_slot:
                raise ValueError("Duplicate destination must be an empty playable slot")
            self._slot().duplicate_clip_to(target)
        elif action == 'has_stop_button':
            self._slot().has_stop_button = boolean(args[0])
        elif action == 'arm':
            if not self._track.can_be_armed:
                raise ValueError("Track cannot be armed")
            self._track.arm = boolean(args[0])
        else:
            clip = self._clip()
            if action in ('duplicate_loop', 'crop', 'stop_scrub'):
                getattr(clip, action)()
            elif action == 'quantize':
                amount = number(args[1])
                if not 0 <= amount <= 1:
                    raise ValueError("Quantize amount must be 0..1")
                clip.quantize(integer(args[0]), amount)
            elif action == 'scrub':
                clip.scrub(number(args[0]))
            elif action in ('loop_region', 'markers'):
                start, end = map(number, args)
                if start < 0 or end <= start:
                    raise ValueError("Region needs 0 <= start < end")
                left, right = ('loop_start', 'loop_end') if action == 'loop_region' else ('start_marker', 'end_marker')
                # Keep the intermediate region valid when shifting past its old end.
                if start >= getattr(clip, right):
                    setattr(clip, right, end)
                    setattr(clip, left, start)
                else:
                    setattr(clip, left, start)
                    setattr(clip, right, end)
            elif action in ('mute_toggle', 'loop_toggle'):
                prop = 'muted' if action == 'mute_toggle' else 'looping'
                setattr(clip, prop, not getattr(clip, prop))
            else:
                prop, convert = {
                    'name': ('name', str), 'color': ('color', integer),
                    'mute': ('muted', boolean), 'loop': ('looping', boolean),
                    'pitch': ('pitch_coarse', integer), 'detune': ('pitch_fine', number),
                    'gain': ('gain', number), 'warping': ('warping', boolean),
                    'warp_mode': ('warp_mode', integer), 'launch_mode': ('launch_mode', integer),
                    'launch_quantization': ('launch_quantization', integer),
                    'legato': ('legato', boolean)}[action]
                value = convert(args[0])
                limits = {'color': (0, 0xffffff), 'pitch': (-48, 48), 'detune': (-50, 49),
                          'gain': (0, 1), 'warp_mode': (0, 6), 'launch_mode': (0, 3),
                          'launch_quantization': (0, 14)}
                if action in limits and not limits[action][0] <= value <= limits[action][1]:
                    raise ValueError("Value out of range for " + action)
                setattr(clip, prop, value)

    def update_display(self):
        super(SingleSlotOSC, self).update_display()
        if not self._alive:
            return
        try:
            self._reconcile()
            selection = (self.song().view.selected_track, self.song().view.selected_scene)
            if self._follow and selection != self._last_selection:
                self._last_selection = selection
                if selection[0] in self._tracks():
                    self._dispatch('/focus/adopt_selection', [])
            if self._socket is not None:
                for _ in range(config.MAX_PACKETS_PER_TICK):
                    try:
                        data, sender = self._socket.recvfrom(65535)
                    except OSError as error:
                        if error.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                            self.log_message('SingleSlotOSC receive error: %s' % error)
                        break
                    self._peer = (config.REPLY_HOST or sender[0], config.REPLY_PORT)
                    try:
                        messages = decode(data, config.MAX_MESSAGES_PER_PACKET)
                    except Exception as error:
                        self._send('/focus/error', ['packet', str(error)])
                        continue
                    for address, args in messages:
                        try:
                            self._dispatch(address, args)
                            self._send('/focus/ok', [address])
                            self._broadcast_state(force=True)
                        except Exception as error:
                            self._send('/focus/error', [address, str(error)])
            self._ticks += 1
            if self._ticks % config.STATE_INTERVAL_TICKS == 0:
                self._broadcast_state()
        except Exception as error:
            self.log_message('SingleSlotOSC update error: %s' % error)

    def disconnect(self):
        self._alive = False
        self._release()
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        self._c_instance.set_session_highlight(-1, -1, -1, -1, False)
        super(SingleSlotOSC, self).disconnect()

    def refresh_state(self):
        super(SingleSlotOSC, self).refresh_state()
        if hasattr(self, '_layout'):
            self._layout = None
