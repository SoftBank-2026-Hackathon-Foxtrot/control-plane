"""Create only the receipts bucket using SigV4; no application objects are written."""
import datetime
import hashlib
import hmac
import json
from pathlib import Path
import urllib.error
import urllib.request


def request(method, address, key, secret):
    host, path = address + ":8333", "/receipts"
    now = datetime.datetime.now(datetime.timezone.utc)
    date, stamp = now.strftime("%Y%m%d"), now.strftime("%Y%m%dT%H%M%SZ")
    payload = hashlib.sha256(b"").hexdigest()
    headers = f"host:{host}\nx-amz-content-sha256:{payload}\nx-amz-date:{stamp}\n"
    names = "host;x-amz-content-sha256;x-amz-date"
    canonical = "\n".join([method, path, "", headers, names, payload])
    scope = f"{date}/us-east-1/s3/aws4_request"
    signed = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    signing_key = ("AWS4" + secret).encode()
    for part in (date, "us-east-1", "s3", "aws4_request"):
        signing_key = hmac.new(signing_key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(signing_key, signed.encode(), hashlib.sha256).hexdigest()
    req = urllib.request.Request("http://" + host + path, method=method,
                                 data=b"" if method == "PUT" else None,
                                 headers={"x-amz-date": stamp, "x-amz-content-sha256": payload,
                                          "Authorization": f"AWS4-HMAC-SHA256 Credential={key}/{scope}, SignedHeaders={names}, Signature={signature}"})
    # This registered private endpoint must not go through controller/host HTTP proxies.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    config = json.loads((root / "host.json").read_text())
    key = (root / "secrets/object_admin_key").read_text()
    secret = (root / "secrets/object_admin_secret").read_text()
    status = request("HEAD", config["serviceAddress"], key, secret)
    import sys
    if status == 404 and "--check" not in sys.argv:
        status = request("PUT", config["serviceAddress"], key, secret)
    if status != 200:
        raise SystemExit("Bucket preparation failed")
    print("receipts bucket ready")
