"""Standalone connection pages, including browser errors, with no external assets."""

from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRoute
from jinja2 import Environment, FileSystemLoader, select_autoescape

from agentbox.api.errors import APIError

templates = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=select_autoescape())


def page(view, *, status_code=200, **context):
    response = HTMLResponse(templates.get_template("connect.html").render(view=view, **context), status_code=status_code)
    response.headers.update({
        "Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; "
                                   "form-action 'self' https://accounts.google.com https://login.microsoftonline.com; "
                                   "frame-ancestors 'none'; base-uri 'none'",
    })
    return response


ERRORS = {
    "not_found": ("Не удалось открыть ссылку", "Откройте полную ссылку, которую передал агент."),
    "link_expired": ("Нужна новая ссылка", "Эта ссылка уже использована или срок её действия истёк. Попросите агента создать новую."),
    "csrf_invalid": ("Обновите страницу", "Сессия формы изменилась. Вернитесь к выбору подключения и попробуйте ещё раз."),
    "oauth_state_invalid": ("Не удалось подтвердить сессию", "Вернитесь к выбору подключения и повторите вход в этом браузере."),
    "oauth_not_configured": ("Подключение пока недоступно", "Владелец Agentbox ещё не настроил вход через почтовый сервис."),
    "managed_delivery_unavailable": ("Почта агента пока недоступна", "Владелец Agentbox ещё не завершил настройку внешней доставки."),
    "gmail_permissions_invalid": ("Нужны разрешения на чтение и отправку", "Разрешите оба действия на странице Google. Если раньше вы давали Agentbox полный доступ, сначала удалите его в настройках аккаунта Google."),
    "mailbox_login_failed": ("Не удалось подключить ящик", "Проверьте адрес, пароль приложения и настройки почтового сервиса."),
    "conflict": ("Адрес уже используется", "Выберите другой адрес или откройте существующий ящик в Agentbox."),
    "validation_error": ("Проверьте данные", "Проверьте адрес и значения полей, затем попробуйте ещё раз."),
    "app_password_disabled": ("Пароли приложений отключены", "Используйте вход через почтовый сервис. Настройки самостоятельного развёртывания доступны владельцу Agentbox."),
}


class BrowserRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request):
            try:
                return await handler(request)
            except (APIError, httpx.HTTPError) as exc:
                if not request.url.path.startswith("/connect/") or request.url.path.endswith("/session"):
                    raise
                code = exc.code if isinstance(exc, APIError) else "network_error"
                title, detail = ERRORS.get(code, ("Подключение не завершено", "Не удалось подтвердить доступ к почте. Вернитесь к выбору подключения и попробуйте ещё раз."))
                link_id = request.path_params.get("link_id") or request.query_params.get("state", "").partition(".")[0]
                retry_url = "/connect/" + quote(link_id, safe="") if link_id and code not in {"not_found", "link_expired"} else None
                return page("error", title=title, detail=detail, retry_url=retry_url,
                            status_code=exc.status if isinstance(exc, APIError) else 502)

        return handle
