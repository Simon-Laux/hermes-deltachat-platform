"""Data classes and types from the JSON-RPC."""

from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Optional

from ._utils import AttrDict


class ContactFlag(IntEnum):
    """Contact flags used for filtering"""

    VERIFIED_ONLY = 0x01
    ADD_SELF = 0x02


class ChatlistFlag(IntEnum):
    """Chatlist flags used for filtering"""

    ARCHIVED_ONLY = 0x01
    NO_SPECIALS = 0x02
    ADD_ALLDONE_HINT = 0x04
    FOR_FORWARDING = 0x08


class SpecialContactId(IntEnum):
    """Special contact IDs"""

    SELF = 1
    INFO = 2  # centered messages as "member added", used in all chats
    DEVICE = 5  #  messages "update info" in the device-chat
    LAST_SPECIAL = 9


class EventType(str, Enum):
    """Core event types"""

    INFO = "Info"
    SMTP_CONNECTED = "SmtpConnected"
    IMAP_CONNECTED = "ImapConnected"
    SMTP_MESSAGE_SENT = "SmtpMessageSent"
    IMAP_MESSAGE_DELETED = "ImapMessageDeleted"
    IMAP_MESSAGE_MOVED = "ImapMessageMoved"
    IMAP_INBOX_IDLE = "ImapInboxIdle"
    NEW_BLOB_FILE = "NewBlobFile"
    DELETED_BLOB_FILE = "DeletedBlobFile"
    WARNING = "Warning"
    ERROR = "Error"
    ERROR_SELF_NOT_IN_GROUP = "ErrorSelfNotInGroup"
    MSGS_CHANGED = "MsgsChanged"
    REACTIONS_CHANGED = "ReactionsChanged"
    INCOMING_REACTION = "IncomingReaction"
    INCOMING_MSG = "IncomingMsg"
    INCOMING_MSG_BUNCH = "IncomingMsgBunch"
    MSGS_NOTICED = "MsgsNoticed"
    MSG_DELIVERED = "MsgDelivered"
    MSG_FAILED = "MsgFailed"
    MSG_READ = "MsgRead"
    MSG_DELETED = "MsgDeleted"
    CHAT_MODIFIED = "ChatModified"
    CHAT_EPHEMERAL_TIMER_MODIFIED = "ChatEphemeralTimerModified"
    CONTACTS_CHANGED = "ContactsChanged"
    LOCATION_CHANGED = "LocationChanged"
    CONFIGURE_PROGRESS = "ConfigureProgress"
    IMEX_PROGRESS = "ImexProgress"
    IMEX_FILE_WRITTEN = "ImexFileWritten"
    SECUREJOIN_INVITER_PROGRESS = "SecurejoinInviterProgress"
    SECUREJOIN_JOINER_PROGRESS = "SecurejoinJoinerProgress"
    CONNECTIVITY_CHANGED = "ConnectivityChanged"
    SELFAVATAR_CHANGED = "SelfavatarChanged"
    WEBXDC_STATUS_UPDATE = "WebxdcStatusUpdate"
    WEBXDC_INSTANCE_DELETED = "WebxdcInstanceDeleted"
    INCOMING_CALL = "IncomingCall"
    INCOMING_CALL_ACCEPTED = "IncomingCallAccepted"
    OUTGOING_CALL_ACCEPTED = "OutgoingCallAccepted"
    CALL_ENDED = "CallEnded"


class ChatType(str, Enum):

    SINGLE = "Single"
    GROUP = "Group"
    MAILINGLIST = "Mailinglist"
    OUT_BROADCAST = "OutBroadcast"
    IN_BROADCAST = "InBroadcast"


class ChatVisibility(str, Enum):
    """Chat visibility types"""

    NORMAL = "Normal"
    ARCHIVED = "Archived"
    PINNED = "Pinned"


class DownloadState(str, Enum):

    DONE = "Done"
    AVAILABLE = "Available"
    FAILURE = "Failure"
    UNDECIPHERABLE = "Undecipherable"
    IN_PROGRESS = "InProgress"


class MessageViewtype(str, Enum):
    """Message view type."""

    UNKNOWN = "Unknown"
    TEXT = "Text"
    IMAGE = "Image"
    GIF = "Gif"
    STICKER = "Sticker"
    AUDIO = "Audio"
    VOICE = "Voice"
    VIDEO = "Video"
    FILE = "File"
    VIDEOCHAT_INVITATION = "VideochatInvitation"
    WEBXDC = "Webxdc"


class SystemMessageType(str, Enum):
    """System message type."""

    UNKNOWN = "Unknown"
    GROUP_NAME_CHANGED = "GroupNameChanged"
    GROUP_IMAGE_CHANGED = "GroupImageChanged"
    MEMBER_ADDED_TO_GROUP = "MemberAddedToGroup"
    MEMBER_REMOVED_FROM_GROUP = "MemberRemovedFromGroup"
    AUTOCRYPT_SETUP_MESSAGE = "AutocryptSetupMessage"
    SECUREJOIN_MESSAGE = "SecurejoinMessage"
    LOCATION_STREAMING_ENABLED = "LocationStreamingEnabled"
    LOCATION_ONLY = "LocationOnly"
    CHAT_PROTECTION_ENABLED = "ChatProtectionEnabled"
    CHAT_PROTECTION_DISABLED = "ChatProtectionDisabled"
    WEBXDC_STATUS_UPDATE = "WebxdcStatusUpdate"
    EPHEMERAL_TIMER_CHANGED = "EphemeralTimerChanged"
    MULTI_DEVICE_SYNC = "MultiDeviceSync"
    WEBXDC_INFO_MESSAGE = "WebxdcInfoMessage"


class MessageState(IntEnum):
    """State of the message."""

    UNDEFINED = 0
    IN_FRESH = 10
    IN_NOTICED = 13
    IN_SEEN = 16
    OUT_PREPARING = 18
    OUT_DRAFT = 19
    OUT_PENDING = 20
    OUT_FAILED = 24
    OUT_DELIVERED = 26
    OUT_MDN_RCVD = 28


class SpecialMessageId(IntEnum):
    """Special message ids"""

    DAYMARKER = 9
    LAST_SPECIAL = 9


class EnteredCertificateChecks(str, Enum):
    # `Automatic` means that provider database setting should be taken. If there is no provider database setting for certificate checks, check certificates strictly.
    AUTOMATIC = "automatic"

    # Ensure that TLS certificate is valid for the server hostname.
    STRICT = "strict"

    # Accept certificates that are expired, self-signed or otherwise not valid for the server hostname.
    ACCEPT_INVALID_CERTIFICATES = "acceptInvalidCertificates"


class Connectivity(IntEnum):
    """Connectivity states"""

    NOT_CONNECTED = 1000
    CONNECTING = 2000
    WORKING = 3000
    CONNECTED = 4000


class KeyGenType(IntEnum):
    """Type of the key to generate"""

    DEFAULT = 0
    RSA2048 = 1
    ED25519 = 2
    RSA4096 = 3


# "Lp" means "login parameters"
class LpAuthFlag(IntEnum):
    """Authorization flags"""

    OAUTH2 = 0x2
    NORMAL = 0x4


class MediaQuality(IntEnum):
    """Media quality setting"""

    BALANCED = 0
    WORSE = 1


class ProviderStatus(IntEnum):
    """Provider status according to manual testing"""

    OK = 1
    PREPARATION = 2
    BROKEN = 3


class PushNotifyState(IntEnum):
    """Push notifications state"""

    NOT_CONNECTED = 0
    HEARTBEAT = 1
    CONNECTED = 2


class ShowEmails(IntEnum):
    """Show emails mode"""

    OFF = 0
    ACCEPTED_CONTACTS = 1
    ALL = 2


class SocketSecurity(IntEnum):
    """Socket security"""

    AUTOMATIC = 0
    SSL = 1
    STARTTLS = 2
    PLAIN = 3


class VideochatType(IntEnum):
    """Video chat URL type"""

    UNKNOWN = 0
    BASICWEBRTC = 1
    JITSI = 2


class CoreEvent(AttrDict):
    """Delta Chat core Event"""

    kind: EventType


@dataclass
class Event:
    """Low level RPC event."""

    account_id: int
    event: CoreEvent


@dataclass
class NewMsgEvent:
    """New message bot-specific event"""

    command: str
    payload: str
    msg: "Message"


@dataclass
class MsgData:
    """Message data provided to Rpc.send_msg()"""

    text: Optional[str] = None
    html: Optional[str] = None
    viewtype: Optional[MessageViewtype] = None
    file: Optional[str] = None
    location: Optional[tuple[float, float]] = None
    override_sender_name: Optional[str] = None
    quoted_message_id: Optional[int] = None


class Message(AttrDict):
    """Message snapshot"""
