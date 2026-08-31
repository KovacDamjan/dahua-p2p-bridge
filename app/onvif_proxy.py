import argparse
import asyncio
import re

async def handle(client, upstream_host, upstream_port, public_host, public_port):
    try:
        reader, writer = await asyncio.open_connection(upstream_host, upstream_port)
        request = await client.read(1024 * 1024)
        if not request:
            writer.close(); await writer.wait_closed(); return
        writer.write(request); await writer.drain()
        response = await reader.read(8 * 1024 * 1024)
        body_sep = response.find(b"\\r\\n\\r\\n")
        if body_sep >= 0:
            head, body = response[:body_sep], response[body_sep + 4:]
            body_text = body.decode("utf-8", errors="replace")
            body_text = re.sub(r'http://[^/\\s<>" ]+:80', f"http://{public_host}:{public_port}", body_text)
            body_text = re.sub(r"https?://(?:192\\.168\\.[0-9.]+|10\\.[0-9.]+|172\\.(?:1[6-9]|2[0-9]|3[0-1])\\.[0-9.]+)(?::\\d+)?", f"http://{public_host}:{public_port}", body_text)
            body = body_text.encode("utf-8")
            head = re.sub(br"Content-Length:\\s*\\d+", f"Content-Length: {len(body)}".encode(), head, count=1, flags=re.I)
            response = head + b"\\r\\n\\r\\n" + body
        client.write(response); await client.drain()
    except Exception:
        pass
    finally:
        try: writer.close(); await writer.wait_closed()
        except Exception: pass
        client.close(); await client.wait_closed()

async def main(listen_host, listen_port, upstream_port, public_host):
    server = await asyncio.start_server(lambda r,w: handle(w, "127.0.0.1", upstream_port, public_host, listen_port), listen_host, listen_port)
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--listen-host", default="0.0.0.0")
    p.add_argument("--listen-port", type=int, required=True)
    p.add_argument("--upstream-port", type=int, required=True)
    p.add_argument("--public-host", required=True)
    a = p.parse_args()
    asyncio.run(main(a.listen_host, a.listen_port, a.upstream_port, a.public_host))
