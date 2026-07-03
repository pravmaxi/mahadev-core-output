from http.server import HTTPServer, BaseHTTPRequestHandler
from kiteconnect import KiteConnect
import webbrowser
import urllib.parse
import threading
import time
import sys

API_KEY = "ud3fa01mv5n39ra2"
API_SECRET = "0pwk1hfa1ljb0mslm3apazf7mdupwp3r"
PORT = 5000

class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)


        
        query = urllib.parse.parse_qs(parsed.query)
        request_token = query.get('request_token', [None])[0]

        # If we have a token, process it immediately (regardless of the path)
        if request_token:
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(b"<html><body><h3>Token received. You can close this window.</h3></body></html>")
            print(f"\n✅ Request token received: {request_token}")

            # Generate access token
            kite = KiteConnect(api_key=API_KEY)
            try:
                data = kite.generate_session(request_token, api_secret=API_SECRET)
                access_token = data["access_token"]
                print(f"✅ Access Token: {access_token}\n")
                with open("access_token.txt", "w") as f:
                    f.write(access_token)
            except Exception as e:
                print(f"❌ Error generating token: {e}")
                return

            # Shut down the server from a separate thread (prevents deadlock)
            def shutdown_server():
                time.sleep(0.5)   # ensure the response is fully sent
                self.server.shutdown()
                print("Server stopped.")

            threading.Thread(target=shutdown_server, daemon=True).start()

        else:
            # No token – serve the home page (and open login URL)
            login_url = f"https://kite.zerodha.com/connect/login?api_key={API_KEY}&v=3"
            webbrowser.open(login_url)
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(f"""
            <html>
            <body>
                <h3>Kite Connect Token Generator</h3>
                <p>Login page opened in your browser.</p>
                <p>If not, <a href='{login_url}'>click here</a>.</p>
                <p>After login, you will be redirected back here automatically.</p>
                <p>Make sure your Kite app redirect URL is set to:<br>
                <code>http://127.0.0.1:{PORT}/callback</code></p>
            </body>
            </html>
            """.encode())

    def log_message(self, format, *args):
        pass  # suppress default logging

def open_browser():
    login_url = f"https://kite.zerodha.com/connect/login?api_key={API_KEY}&v=3"
    webbrowser.open(login_url)

if __name__ == "__main__":
    print(f"Starting server on http://127.0.0.1:{PORT}")
    print(f"Set your Kite app redirect URL to: http://127.0.0.1:{PORT}/callback")
    print("Opening login page in your default browser...")

    server = HTTPServer(('127.0.0.1', PORT), CallbackHandler)
    threading.Timer(1.0, open_browser).start()   # open login page after 1 sec

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped by user.")
    finally:
        server.server_close()
        print("Exiting.")
        sys.exit(0)