from http.server import HTTPServer, BaseHTTPRequestHandler
from kiteconnect import KiteConnect
import webbrowser
import urllib.parse
import os

API_KEY = "ud3fa01mv5n39ra2"
API_SECRET = "0pwk1hfa1ljb0mslm3apazf7mdupwp3r"
PORT = 5000

class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/callback':
            query = urllib.parse.parse_qs(parsed.query)
            request_token = query.get('request_token', [None])[0]
            if request_token:
                self.send_response(200)
                self.send_header('Content-type', 'text/html')
                self.end_headers()
                self.wfile.write(b"<html><body><h3>Token received. You can close this window.</h3></body></html>")
                print(f"\n✅ Request token received: {request_token}")
                # Generate access token
                kite = KiteConnect(api_key=API_KEY)
                data = kite.generate_session(request_token, api_secret=API_SECRET)
                access_token = data["access_token"]
                print(f"✅ Access Token: {access_token}\n")
                with open("access_token.txt", "w") as f:
                    f.write(access_token)
                # Shutdown server
                self.server.shutdown()
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Missing request_token")
        else:
            # Home page – open login URL
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

if __name__ == "__main__":
    print(f"Starting server on http://127.0.0.1:{PORT}")
    print(f"Set your Kite app redirect URL to: http://127.0.0.1:{PORT}/callback")
    server = HTTPServer(('127.0.0.1', PORT), CallbackHandler)
    server.serve_forever()