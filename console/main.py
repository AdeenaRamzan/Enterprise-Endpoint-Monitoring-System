import sys
from PyQt6.QtWidgets import QApplication

from api_client import ApiClient
from login_window import LoginWindow
from it_console import run_it_console
from manager_console import run_manager_console
from session_store import save_session, load_session, clear_session
from theme import apply_theme


def route(api):
    save_session(api)
    if api.role in ("SuperAdmin", "ITStaff"):
        return run_it_console(api)
    else:
        # Manager and Viewer both get the simplified, restricted UI.
        # The backend RBAC (require_gallery/require_read) is what actually
        # enforces what a Viewer can fetch -- this UI just doesn't offer
        # anything it can't back up server-side.
        return run_manager_console(api)


def try_resume_session():
    session = load_session()
    if not session:
        return None
    api = ApiClient(base_url=session["base_url"])
    api.token = session["token"]
    api.role = session["role"]
    api.display_name = session["display_name"]
    api.username = session["username"]
    try:
        api.get_status()  # cheap call to confirm the token still works
        return api
    except Exception:
        clear_session()
        return None


def main():
    app = QApplication(sys.argv)
    apply_theme(app)

    resumed = try_resume_session()
    if resumed:
        window_app, window = route(resumed)
        sys.exit(app.exec())
        return

    holder = {}

    def on_success(api):
        holder["app_window"] = route(api)

    login = LoginWindow(on_success)
    login.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
