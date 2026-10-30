"""凭据字段与常见 URL 凭据的输出脱敏，不修改发送给服务端的协议。"""
import os
import re


def redact_text(value: str, secrets=()) -> str:
    text = str(value)
    known = [*secrets, *(v for k, v in os.environ.items() if any(x in k.upper() for x in ("TOKEN", "API_KEY", "WEBHOOK")))]
    for secret in sorted({str(s) for s in known if s}, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)([?&](?:token|authorization|key|api_key)=)[^&\s\"']+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(Bearer\s+)[^\s\"']+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(https?://)[^/\s@]+@", r"\1[REDACTED]@", text)
    text = re.sub(r"(?i)([\"']?(?:token|api_key|authorization|webhook)[\"']?\s*[:=]\s*[\"'])[^\"']*", r"\1[REDACTED]", text)
    return text


def redact_data(value, secrets=()):
    if isinstance(value, dict):
        return {k: "[REDACTED]" if any(s in k.lower() for s in ("token", "api_key", "authorization", "webhook"))
                else redact_data(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_data(v, secrets) for v in value]
    return redact_text(value, secrets) if isinstance(value, str) else value


class SafeLog:
    """按完整行脱敏，防止 print 的多次 write 拆开凭据。"""
    def __init__(self, stream, secrets=()):
        self.stream, self.secrets, self.buffer = stream, secrets, ""

    def write(self, text):
        self.buffer += text
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            self.stream.write(redact_text(line, self.secrets) + "\n")
        return len(text)

    def flush(self):
        self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)
