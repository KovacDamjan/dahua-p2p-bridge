import argparse
import asyncio
import re

async def handle(client, upstream_host, upstream_port, public_host, public_port):
    try:
        reader, writer = await asyncio.open_connection(upstream_host, upstream_port)
        request = await reader.read(1024 * 1024)
        if not request:
            writer.close(); await writer.wait_closed(); return
        upstream = await asyncio.open_connection(upstream_host, upstream_port)
    except Exception:
        return
    # The first connection above is unused; reconnect cleanly for request/response.
    try:
        ureader, uwriter = upstream
        uwriter.write(request); await uwriter.drain()
        response = await ureader.read(8 * 1024 * 1024)
        text = response.decode("utf-8", errors="replace")
        text = re.sub(r"http://[^/\s<>"]+:80", f"http://{public_host}:{public_port}", text)
        text = re.sub(r"https?://(?:192\.168\.[0-9.]+|10\.[0-9.]+|172\.(?:1[6-9]|2[0-9]|3[0-1])\.[0-9.]+)(?::\d+)?", f"http://{public_host}:{public_port}", text)
        response = text.encode("utf-8")
        m = re.search(br"Content-Length:\s*(\d+)", response, re.I)
        if m:
            response = re.sub(br"Content-Length:\s*\d+", f"Content-Length: {len(response.split(b'\r\n\r\n',1)[1])}".encode(), response, count=1, flags=re.I)
        writer = client
        writer.write(response); await writer.drain()
    finally:
        uwriter.close(); await uwriter.wait_closed()
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
