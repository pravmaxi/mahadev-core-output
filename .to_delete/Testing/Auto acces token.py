from kiteconnect import KiteConnect
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager
import urllib.parse
import os
import time

API_KEY = "ud3fa01mv5n39ra2"
API_SECRET = "0pwk1hfa1ljb0mslm3apazf7mdupwp3r"
REDIRECT_URI = "http://127.0.0.1:5000/callback"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ACCESS_TOKEN_FILE = os.path.join(SCRIPT_DIR, "access_token.txt")
WAIT_SECONDS = 300


def get_login_url():
    encoded_redirect = urllib.parse.quote(REDIRECT_URI, safe='')
    return (
        f"https://kite.zerodha.com/connect/login?api_key=ud3fa01mv5n39ra2&v=3"
        f"&redirect_uri={encoded_redirect}&v=3"
    )


def parse_request_token(url):
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    return query.get("request_token", [None])[0]


def save_access_token(access_token):
    with open(ACCESS_TOKEN_FILE, "w") as f:
        f.write(access_token)
    print(f"Saved access token to: {ACCESS_TOKEN_FILE}")


def create_driver():
    options = Options()
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-infobars")
    options.add_argument("--start-maximized")
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


def main():
    login_url = get_login_url()
    print("Opening Kite login URL in browser using Selenium...")
    print(login_url)

    driver = None
    try:
        driver = create_driver()
        driver.get(login_url)

        print("Waiting for redirect to callback URL...")
        wait = WebDriverWait(driver, WAIT_SECONDS)
        wait.until(lambda d: "request_token=" in d.current_url)

        current_url = driver.current_url
        print(f"Redirect detected: {current_url}")

        request_token = parse_request_token(current_url)
        if not request_token:
            raise RuntimeError("request_token not found in redirected URL")

        kite = KiteConnect(api_key=API_KEY)
        data = kite.generate_session(request_token, api_secret=API_SECRET)
        access_token = data.get("access_token")
        if not access_token:
            raise RuntimeError("access_token missing from Kite response")

        print(f"\n✅ Access Token: {access_token}\n")
        save_access_token(access_token)
    except Exception as e:
        print(f"Error: {type(e).__name__}: {e}")
    finally:
        if driver:
            time.sleep(2)
            driver.quit()


if __name__ == "__main__":
    main()
