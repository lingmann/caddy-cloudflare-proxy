import hashlib
import http.client
import json
import ssl
import sys
import time

# Trust only the disposable CA from this test. Never disable verification.
for attempt in range(60):
    try:
        context = ssl.create_default_context(cafile="/trust/caddy/pki/authorities/local/root.crt")
        connection = http.client.HTTPSConnection("service.example.com", context=context, timeout=2)
        connection.connect()
        cert = connection.sock.getpeercert(binary_form=True)
        connection.request("GET", "/test?query=kept", headers={"Authorization": "Bearer example-test-token"})
        response = connection.getresponse()
        assert response.status == 200, response.status
        body = json.loads(response.read())
        expected_backend = sys.argv[1] if len(sys.argv) > 1 else "first"
        assert body == {"authorization": "Bearer example-test-token", "proto": "https", "path": "/test?query=kept", "backend": expected_backend}, body
        print(hashlib.sha256(cert).hexdigest())
        connection.close()
        break
    except (OSError, http.client.HTTPException):
        if attempt == 59:
            raise
        time.sleep(0.25)
