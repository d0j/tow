"""Route registration: the split web package must not let a route shadow a later one."""

from fastapi.routing import iter_route_contexts

from tow.web import app


def _routes():
    """Every route in matching order, the routers' included ones flattened."""
    return [
        (method, route)
        for route in iter_route_contexts(app.routes)
        for method in sorted(getattr(route, "methods", None) or [])
    ]


def test_no_route_is_shadowed_by_an_earlier_parameterised_route():
    routes = _routes()
    for index, (method, route) in enumerate(routes):
        if "{" in route.path:
            continue
        shadowing = [
            earlier.path
            for earlier_method, earlier in routes[:index]
            if earlier_method == method and "{" in earlier.path and earlier.path_regex.match(route.path)
        ]
        assert not shadowing, f"{method} {route.path} is shadowed by {shadowing}"


def test_every_route_is_registered_once():
    keys = [(method, route.path) for method, route in _routes()]
    assert len(keys) == len(set(keys))
    for expected in [("GET", "/"), ("GET", "/sites"), ("GET", "/settings"), ("GET", "/doctor"), ("POST", "/undo")]:
        assert expected in keys


# Every (method, path) the app serves, first recorded from v1.19.0 before the web package became
# routers: reorganizing the route modules must not add, drop or rename a URL. A new page adds its
# line here on purpose (docs/EXTENDING.md, "Страницы и маршруты").
ROUTES = frozenset(
    {
        ("GET", "/login"),
        ("POST", "/login"),
        ("POST", "/logout"),
        ("GET", "/healthz"),
        ("GET", "/health.json"),
        ("GET", "/history"),
        ("GET", "/log.json"),
        ("GET", "/topics/{tid}/downloads.json"),
        ("GET", "/favicon.ico"),
        ("GET", "/"),
        ("GET", "/topics/{tid}/edit-panel"),
        ("GET", "/topics/{tid}/edit"),
        ("POST", "/topics/add"),
        ("GET", "/topics/{tid}/tracker-browser-auth"),
        ("POST", "/topics/{tid}/tracker-browser-auth"),
        ("GET", "/topics/{tid}/tracker-browser-auth/status"),
        ("POST", "/topics/{tid}/tracker-login"),
        ("POST", "/topics/{tid}/delete"),
        ("POST", "/undo"),
        ("POST", "/topics/{tid}/edit"),
        ("POST", "/topics/{tid}/pause"),
        ("POST", "/topics/{tid}/replace-revision"),
        ("POST", "/topics/{tid}/check"),
        ("POST", "/check"),
        ("GET", "/check/status"),
        ("GET", "/sites"),
        ("POST", "/topics/guess-title"),
        ("POST", "/sites/guess"),
        ("POST", "/sites/new"),
        ("POST", "/sites/{name}/freeze"),
        ("POST", "/sites/{name}/delete"),
        ("POST", "/sites/{name}"),
        ("POST", "/sites/{name}/login"),
        ("POST", "/sites/{name}/probe"),
        ("POST", "/sites/{name}/prefer"),
        ("GET", "/doctor"),
        ("POST", "/doctor/run"),
        ("GET", "/settings"),
        ("POST", "/settings/language"),
        ("POST", "/settings/backup/location"),
        ("POST", "/settings/backup/now"),
        ("POST", "/settings/backup/night/{name}/restore"),
        ("POST", "/settings/client/add"),
        ("POST", "/settings/client/remove"),
        ("POST", "/settings/client/default"),
        ("POST", "/settings/access"),
        ("GET", "/settings/service/status.json"),
        ("POST", "/settings/service/autostart"),
        ("POST", "/settings/service/restart"),
        ("POST", "/settings/restore-points"),
        ("POST", "/settings/restore-points/{point_id}/restore"),
        ("POST", "/settings/portable/export"),
        ("POST", "/settings/portable/import"),
        ("GET", "/settings/help"),
        ("POST", "/settings/client"),
        ("POST", "/settings/notifier/{kind}"),
        ("POST", "/settings/notifier/{kind}/test"),
        ("POST", "/settings/notifier/{kind}/remove"),
        ("POST", "/settings/client/ping"),
        ("POST", "/settings/interval"),
        ("GET", "/setup"),
        ("POST", "/setup"),
        ("POST", "/settings/password"),
        ("POST", "/settings/sessions/sign-out"),
    }
)


def test_the_routes_are_the_recorded_ones():
    assert {(method, route.path) for method, route in _routes()} == ROUTES
    # Besides the routes, only the static files are mounted.
    assert [route.path for route in iter_route_contexts(app.routes) if not getattr(route, "methods", None)] == [
        "/static"
    ]
