# Restart Live after changes. For LAN control use 0.0.0.0 and a trusted LAN.
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 9000
REPLY_PORT = 9001
# None replies to the sender's IP at REPLY_PORT (not its ephemeral source port).
REPLY_HOST = None
MAX_PACKETS_PER_TICK = 32
MAX_MESSAGES_PER_PACKET = 64
STATE_INTERVAL_TICKS = 5
