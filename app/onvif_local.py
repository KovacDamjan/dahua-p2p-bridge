import html
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


SOAP = "http://www.w3.org/2003/05/soap-envelope"
ONVIF = "http://www.onvif.org/ver10/schema"
TDS = "http://www.onvif.org/ver10/device/wsdl"
TRT = "http://www.onvif.org/ver10/media/wsdl"
TEV = "http://www.onvif.org/ver10/events/wsdl"


def _soap(body: str) -> bytes:
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="{SOAP}" xmlns:tds="{TDS}" xmlns:trt="{TRT}" xmlns:tev="{TEV}" xmlns:tt="{ONVIF}">
<s:Body>{body}</s:Body></s:Envelope>'''.encode()


def _host(handler) -> str:
    return handler.headers.get("Host", "127.0.0.1").split(":", 1)[0]


def _uri(handler, port: int, subtype: int) -> str:
    return f"rtsp://{_host(handler)}:{port}/cam/realmonitor?channel=1&subtype={subtype}"


def _requested_subtype(data: bytes) -> int:
    text = data.decode("utf-8", errors="ignore").lower()
    # Synology normally sends the profile token as an XML attribute:
    # <trt:ProfileToken>Profile_Sub</trt:ProfileToken> or token="Profile_Sub".
    attribute_match = re.search(
        r"""(?:profiletoken|profile_token)[^>]*?\btoken\s*=\s*["']([^"']+)["']""",
        text,
    )
    element_match = re.search(
        r"<(?:[^:>]+:)?profiletoken[^>]*>\s*([^<]+)",
        text,
    )
    token = (
        attribute_match.group(1).strip()
        if attribute_match
        else element_match.group(1).strip()
        if element_match
        else ""
    )
    is_sub = (
        "profile_sub" in token
        or "substream" in token
        or "videoencoder_sub" in text
        or "profile_sub" in text
    )
    return 1 if is_sub else 0

def _action(data: bytes) -> str:
    body = re.search(rb"<(?:[^:>]+:)?Body(?:\s|>)", data)
    search = data[body.end():] if body else data
    match = re.search(rb"<(?:[^:>]+:)?([A-Za-z0-9]+)(?:\s|>)", search)
    return match.group(1).decode() if match else ""


class _Handler(BaseHTTPRequestHandler):
    server_version = "DahuaP2PBridgeONVIF/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        return

    def _is_onvif_path(self):
        return self.path.split("?", 1)[0] in (
            "/", "/onvif/device_service", "/onvif/media_service",
            "/onvif/events_service",
        )

    def _send_health(self, include_body=True):
        body = b'<?xml version="1.0"?><onvif>online</onvif>'
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def do_HEAD(self):
        if self._is_onvif_path():
            self._send_health(include_body=False)
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        if self._is_onvif_path():
            self.send_response(200)
            self.send_header("Allow", "GET, HEAD, OPTIONS, POST")
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
        else:
            self.send_error(404)

    def do_GET(self):
        if self._is_onvif_path():
            self._send_health()
            return
        self.send_error(404)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            request = self.rfile.read(length)
            action = _action(request)
            print("[ONVIF] {} {} action={}".format(
                self.command, self.path, action or "unknown"
            ), flush=True)
            port = self.server.rtsp_port
            if action == "GetCapabilities":
                body = f"""<tds:GetCapabilitiesResponse><tds:Capabilities>
<tt:Device><tt:XAddr>http://{_host(self)}:{self.server.server_port}/onvif/device_service</tt:XAddr></tt:Device>
<tt:Media><tt:XAddr>http://{_host(self)}:{self.server.server_port}/onvif/media_service</tt:XAddr></tt:Media>
<tt:Events><tt:XAddr>http://{_host(self)}:{self.server.server_port}/onvif/events_service</tt:XAddr><tt:WSPullPointSupport>true</tt:WSPullPointSupport></tt:Events>
</tds:Capabilities></tds:GetCapabilitiesResponse>"""
            elif action == "GetDeviceInformation":
                body = """<tds:GetDeviceInformationResponse><tds:Manufacturer>Dahua</tds:Manufacturer>
<tds:Model>Dahua P2P Bridge</tds:Model><tds:FirmwareVersion>6.7.33</tds:FirmwareVersion>
<tds:SerialNumber> P2P </tds:SerialNumber><tds:HardwareId>P2P</tds:HardwareId></tds:GetDeviceInformationResponse>"""
            elif action == "GetProfiles":
                body = """<trt:GetProfilesResponse>
<trt:Profiles token="Profile_Main" fixed="true"><tt:Name>MainStream</tt:Name>
<tt:VideoSourceConfiguration token="VideoSourceConfig_Main"><tt:Name>MainVideoSource</tt:Name><tt:UseCount>1</tt:UseCount><tt:SourceToken>VideoSource_1</tt:SourceToken><tt:Bounds x="0" y="0" width="3840" height="2160"/></tt:VideoSourceConfiguration>
<tt:VideoEncoderConfiguration token="VideoEncoder_Main"><tt:Name>MainEncoder</tt:Name><tt:UseCount>1</tt:UseCount><tt:Encoding>H264</tt:Encoding><tt:Resolution><tt:Width>3840</tt:Width><tt:Height>2160</tt:Height></tt:Resolution><tt:Quality>5</tt:Quality><tt:RateControl><tt:FrameRateLimit>25</tt:FrameRateLimit><tt:EncodingInterval>1</tt:EncodingInterval><tt:BitrateLimit>8192</tt:BitrateLimit></tt:RateControl><tt:SessionTimeout>PT60S</tt:SessionTimeout></tt:VideoEncoderConfiguration>
</trt:Profiles>
<trt:Profiles token="Profile_Sub" fixed="true"><tt:Name>SubStream</tt:Name>
<tt:VideoSourceConfiguration token="VideoSourceConfig_Sub"><tt:Name>SubVideoSource</tt:Name><tt:UseCount>1</tt:UseCount><tt:SourceToken>VideoSource_1</tt:SourceToken><tt:Bounds x="0" y="0" width="704" height="576"/></tt:VideoSourceConfiguration>
<tt:VideoEncoderConfiguration token="VideoEncoder_Sub"><tt:Name>SubEncoder</tt:Name><tt:UseCount>1</tt:UseCount><tt:Encoding>H264</tt:Encoding><tt:Resolution><tt:Width>704</tt:Width><tt:Height>576</tt:Height></tt:Resolution><tt:Quality>3</tt:Quality><tt:RateControl><tt:FrameRateLimit>10</tt:FrameRateLimit><tt:EncodingInterval>1</tt:EncodingInterval><tt:BitrateLimit>1024</tt:BitrateLimit></tt:RateControl><tt:SessionTimeout>PT60S</tt:SessionTimeout></tt:VideoEncoderConfiguration>
</trt:Profiles>
</trt:GetProfilesResponse>"""
            elif action == "GetStreamUri":
                subtype = _requested_subtype(request)
                print("[ONVIF] GetStreamUri profile={} subtype={}".format("Sub" if subtype else "Main", subtype), flush=True)
                body = f"""<trt:GetStreamUriResponse><trt:MediaUri><tt:Uri>{html.escape(_uri(self, port, subtype))}</tt:Uri>
<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect><tt:InvalidAfterReboot>false</tt:InvalidAfterReboot><tt:Timeout>PT60S</tt:Timeout></trt:MediaUri></trt:GetStreamUriResponse>"""
            elif action == "GetSnapshotUri":
                subtype = 1 if b"Profile_Sub" in request else 0
                body = f"""<trt:GetSnapshotUriResponse><trt:MediaUri><tt:Uri>http://{_host(self)}:{self.server.server_port}/snapshot?subtype={subtype}</tt:Uri></trt:MediaUri></trt:GetSnapshotUriResponse>"""
            elif action == "GetSystemDateAndTime":
                body = """<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime><tt:UTCDateTime><tt:Time><tt:Hour>0</tt:Hour><tt:Minute>0</tt:Minute><tt:Second>0</tt:Second></tt:Time><tt:Date><tt:Year>2026</tt:Year><tt:Month>1</tt:Month><tt:Day>1</tt:Day></tt:Date></tt:UTCDateTime></tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>"""
            elif action in ("CreatePullPointSubscription", "CreateSubscription"):
                body = f"""<tev:CreatePullPointSubscriptionResponse><tev:SubscriptionReference><tt:Address>http://{_host(self)}:{self.server.server_port}/onvif/events_service</tt:Address></tev:SubscriptionReference><tev:CurrentTime>2026-01-01T00:00:00Z</tev:CurrentTime><tev:TerminationTime>2026-01-02T00:00:00Z</tev:TerminationTime></tev:CreatePullPointSubscriptionResponse>"""
            elif action == "PullMessages":
                body = """<tev:PullMessagesResponse><tev:NotificationMessageList/></tev:PullMessagesResponse>"""
            elif action == "GetEventProperties":
                body = """<tev:GetEventPropertiesResponse><tev:TopicNamespaceLocation>http://www.onvif.org/ver10/topics/topicns.xml</tev:TopicNamespaceLocation><tev:TopicSet><tt:MessageDescription/></tev:TopicSet><tev:MessageContentFilterDialect>http://www.onvif.org/ver10/tev/messageContentFilter/MessageContentFilterDialect</tev:MessageContentFilterDialect></tev:GetEventPropertiesResponse>"""
            else:
                body = f"<tds:{action}Response/>"
            response = _soap(body)
            self.send_response(200)
            self.send_header("Content-Type", "application/soap+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(response)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(response)
        except Exception as error:
            print("[ONVIF] request failed: {}".format(error), flush=True)
            self.send_error(500)


class LocalOnvifServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, rtsp_port):
        super().__init__(address, _Handler)
        self.rtsp_port = rtsp_port


def start_onvif(port: int, rtsp_port: int):
    server = LocalOnvifServer(("0.0.0.0", port), rtsp_port)
    thread = threading.Thread(target=server.serve_forever, name=f"onvif-{port}", daemon=True)
    thread.start()
    return server
