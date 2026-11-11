import asyncio
import json
import ssl
from pathlib import Path
from unittest.mock import AsyncMock

import httpcore
import httpx
import pytest

from app.content.llm import FakeLLM
from app.content.site_reader import (
    MAX_BYTES,
    Page,
    PublicOnlyBackend,
    PublicOnlyTransport,
    SiteReadResult,
    decode,
    extract_description,
    find_gtm_containers,
    find_metrica_counters,
    is_public_ip,
    parse_html,
    parse_robots,
    pick_subpages,
    read_site,
    resolve_public,
    rule_matches,
    safe_url,
)


def fixture_transport(fixtures: Path, home: str, requests: list[str]) -> httpx.MockTransport:
    files = {"/": home, "/services": "services.html", "/prices": "prices.html", "/contacts": "contacts.html"}

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        assert request.headers["accept-encoding"] == "identity"
        assert request.headers["user-agent"] == "AdAutopilotBot/0.1"
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:")
        filename = files.get(request.url.path)
        if filename is None:
            return httpx.Response(404)
        return httpx.Response(200, content=(fixtures / filename).read_bytes(), headers={"content-type": "text/html"})

    return httpx.MockTransport(handle)


async def test_site_with_counter(fixtures: Path) -> None:
    requests: list[str] = []
    result = await read_site(
        "https://salon.example/",
        transport=fixture_transport(
            fixtures,
            "site_with_counter.html",
            requests,
        ),
    )
    assert [page.url for page in result.pages] == [
        "https://salon.example/",
        "https://salon.example/services",
        "https://salon.example/prices",
        "https://salon.example/contacts",
    ]
    assert result.counter_ids == [87654321]
    assert result.metrica_counter_id == 87654321
    assert result.phones == ["+78461234567"]
    assert result.emails == ["hello@prityazhenie.example"]
    assert result.can_launch
    assert result.launch_block_reason is None
    assert not result.needs_owner_text
    assert len(requests) == 5
    assert requests[0] == "https://salon.example/robots.txt"
    assert "Бесплатный маникюр" not in result.pages[0].text
    assert "JavaScript" not in result.pages[0].text


async def test_no_counter_blocks_launch(fixtures: Path) -> None:
    requests: list[str] = []
    result = await read_site(
        "https://salon.example",
        transport=fixture_transport(
            fixtures,
            "site_without_counter.html",
            requests,
        ),
    )
    assert result.pages
    assert result.counter_ids == []
    assert result.metrica_counter_id is None
    assert not result.can_launch
    assert "Без сайта со счётчиком" in result.launch_block_reason
    assert result.phones == ["+78461234567"]
    assert result.emails == ["studio@example.org"]


async def test_js_site_requests_owner_text_without_calling_llm(fixtures: Path) -> None:
    requests: list[str] = []
    result = await read_site("https://salon.example", transport=fixture_transport(fixtures, "js_site.html", requests))
    llm = FakeLLM()
    draft = await extract_description(result, llm)
    assert result.needs_owner_text
    assert result.pages[0].text == "Загрузка..."
    assert draft.questions[0].field == "owner_text"
    assert llm.calls == []


async def test_extract_verified_description_from_site(fixtures: Path) -> None:
    result = await read_site(
        "https://salon.example",
        transport=fixture_transport(
            fixtures,
            "site_with_counter.html",
            [],
        ),
    )
    llm = FakeLLM(
        json.dumps(
            {
                "services": [{"name": "Маникюр", "price": 150050, "quote": "Маникюр — 1 500,50 ₽."}],
                "address": {"value": "Самара, ул. Полевая, д. 7", "quote": "Адрес: г. Самара, ул. Полевая, д. 7."},
                "advantages": [{"value": "Работаем ежедневно", "quote": "Работаем ежедневно с 09:00 до 21:00."}],
            },
            ensure_ascii=False,
        )
    )
    draft = await extract_description(result, llm)
    assert draft.source == "site"
    assert draft.questions == []
    assert draft.services[0].price == 150050
    assert draft.services[0].source == "https://salon.example/prices"
    assert draft.medical.level == "none"
    assert len(llm.calls) == 1
    assert len(json.loads(llm.calls[0][1])) == 4


async def test_owner_text_is_the_only_quote_source() -> None:
    site = SiteReadResult([Page("https://salon.example", "Бесплатная парковка. " * 10)])
    llm = FakeLLM(
        json.dumps(
            {
                "services": [{"name": "Ботокс", "price": 100000, "quote": "Ботокс — 1000 рублей."}],
                "address": {"value": "Самара, Полевая 7", "quote": "Адрес: Самара, Полевая 7."},
                "advantages": [{"value": "Бесплатная парковка", "quote": "Бесплатная парковка."}],
            },
            ensure_ascii=False,
        )
    )
    draft = await extract_description(site, llm, owner_text="Ботокс — 1000 рублей. Адрес: Самара, Полевая 7.")
    assert draft.source == "owner_text"
    assert draft.services[0].source == "owner_text"
    assert draft.services[0].price == 100000
    assert draft.address.source == "owner_text"
    assert draft.advantages == []
    assert draft.questions[0].field == "advantages.0"
    assert draft.medical.level == "required"
    assert json.loads(llm.calls[0][1])[0]["source"] == "owner_text"
    assert not site.can_launch


async def test_missing_site_accepts_owner_text_but_still_blocks_launch() -> None:
    site = await read_site("")
    draft = await extract_description(site, FakeLLM(), owner_text="Студия маникюра в Самаре")
    assert draft.source == "owner_text"
    assert not site.can_launch
    assert site.launch_block_reason


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.0.0.1",
        "http://172.16.1.2",
        "http://192.168.0.1",
        "http://169.254.169.254/",
        "http://0.0.0.0",
        "http://100.64.0.1",
        "http://[::1]/",
        "http://[::]/",
        "http://[fd00::1]/",
        "http://[fe80::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[64:ff9b::7f00:1]/",
        "http://[2002:7f00:1::]/",
        "http://[fe80::1%25en0]/",
        "http://224.0.0.1/",
        "http://[ff02::1]/",
        "http://salon.example:8080/",
        "https://salon.example:444/",
        "ftp://salon.example/",
        "file:///etc/passwd",
        "http://user:secret@salon.example/",
        "http://salon.example:invalid/",
        "http://salon.example\\@127.0.0.1/",
        "http://salon.example/\ncontacts",
        "https://",
        "//salon.example",
        "http://[invalid",
        "",
    ],
)
async def test_unsafe_urls_never_reach_transport(url: str) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        pytest.fail("Unsafe request reached the transport")

    assert safe_url(url) is None
    result = await read_site(url, transport=httpx.MockTransport(handle))
    assert result.pages == []


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://salon.example", "https://salon.example/"),
        ("http://salon.example:80/prices#top", "http://salon.example:80/prices"),
        ("https://salon.example:443/", "https://salon.example:443/"),
        ("https://пример.рф", "https://xn--e1afmkfd.xn--p1ai/"),
        ("https://SALON.EXAMPLE./", "https://salon.example/"),
        ("https://[2606:4700:4700::1111]/", "https://[2606:4700:4700::1111]/"),
    ],
)
def test_safe_url_normalization(url: str, expected: str) -> None:
    assert safe_url(url) == expected


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1/private",
        "http://169.254.169.254/",
        "http://[::1]/",
        "http://salon.example:8000/",
        "https://other.example/",
        "http://user:secret@salon.example/",
    ],
)
async def test_unsafe_redirect_is_not_followed(location: str) -> None:
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        assert str(request.url) == "https://salon.example/"
        return httpx.Response(302, headers={"location": location})

    result = await read_site("https://salon.example", transport=httpx.MockTransport(handle))
    assert result.pages == []
    assert requests == ["https://salon.example/robots.txt", "https://salon.example/"]


async def test_unsafe_robots_redirect_blocks_site() -> None:
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/robots.txt"})

    result = await read_site("https://salon.example", transport=httpx.MockTransport(handle))
    assert result.pages == []
    assert requests == ["https://salon.example/robots.txt"]


async def test_safe_redirect_and_robots_at_destination(fixtures: Path) -> None:
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private")
        if request.url.host == "salon.example":
            return httpx.Response(301, headers={"location": "https://www.salon.example/"})
        return httpx.Response(
            200,
            content=(fixtures / "site_without_counter.html").read_bytes(),
            headers={
                "content-type": "text/html",
            },
        )

    result = await read_site("https://salon.example", transport=httpx.MockTransport(handle))
    assert result.pages[0].url == "https://www.salon.example/"
    assert requests == [
        "https://salon.example/robots.txt",
        "https://salon.example/",
        "https://www.salon.example/robots.txt",
        "https://www.salon.example/",
    ]


async def test_redirect_target_must_be_allowed_by_robots() -> None:
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private")
        assert request.url.path == "/"
        return httpx.Response(302, headers={"location": "/private"})

    result = await read_site("https://salon.example", transport=httpx.MockTransport(handle))
    assert result.pages == []
    assert requests == ["/robots.txt", "/"]


@pytest.mark.parametrize("status", [403, 500])
async def test_unavailable_robots_blocks_site(status: int) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/robots.txt"
        return httpx.Response(status)

    assert not (await read_site("https://salon.example", transport=httpx.MockTransport(handle))).pages


async def test_robots_blocks_subpage(fixtures: Path) -> None:
    requests = []
    transport = fixture_transport(fixtures, "site_with_counter.html", [])

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /prices")
        return await transport.handle_async_request(request)

    result = await read_site("https://salon.example", transport=httpx.MockTransport(handle))
    assert len(result.pages) == 3
    assert "/prices" not in requests


def test_robots_wildcards_specific_group_and_allow_precedence() -> None:
    robots = parse_robots("""User-agent: *
Disallow: /
User-agent: AdAutopilotBot
Disallow: /private*
Allow: /private/public$
Disallow: /tie
Allow: /tie
User-agent: AdAutopilotBot
Disallow: /prices
""")
    assert robots.allows("https://salon.example/")
    assert not robots.allows("https://salon.example/private/secret")
    assert robots.allows("https://salon.example/private/public")
    assert not robots.allows("https://salon.example/private/public/more")
    assert robots.allows("https://salon.example/tie")
    assert not robots.allows("https://salon.example/prices")


async def test_five_page_limit_and_contact_priority() -> None:
    requests = []
    html = (
        '<a href="/services">Услуги</a>'
        + "".join(f'<a href="/services/{i}">Услуга {i}</a>' for i in range(10))
        + '<a href="/prices">Прайс</a><a href="/contacts">Контакты</a>'
    )

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        requests.append(request.url.path)
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    result = await read_site("https://salon.example", transport=httpx.MockTransport(handle))
    assert len(result.pages) == len(requests) == 5
    assert requests[:4] == ["/", "/services", "/prices", "/contacts"]


async def test_subpages_can_be_discovered_below_home() -> None:
    pages = {
        "/": '<a href="/services">Услуги</a>',
        "/services": '<a href="/prices">Прайс</a>',
        "/prices": '<a href="/contacts">Контакты</a>',
        "/contacts": "Самара, ул. Полевая 7",
    }

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=pages[request.url.path], headers={"content-type": "text/html"})

    result = await read_site("https://salon.example", transport=httpx.MockTransport(handle))
    assert len(result.pages) == 4


def test_picker_rejects_other_domains_private_urls_and_duplicates() -> None:
    page = Page(
        "https://salon.example/",
        "",
        links=[
            ("https://salon.example/", "Услуги"),
            ("https://other.example/services", "Услуги"),
            ("http://127.0.0.1/contacts", "Контакты"),
            ("https://salon.example:8000/prices", "Прайс"),
            ("https://salon.example/prices#a", "Прайс"),
            ("https://salon.example/prices#b", "Прайс"),
        ],
    )
    assert pick_subpages(page) == ["https://salon.example/prices"]
    assert pick_subpages(page, 0) == []


class ChunkedBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        for _ in range(4):
            yield b"x" * (MAX_BYTES // 3)


@pytest.mark.parametrize("mode", ["content_length", "stream", "compressed", "not_html", "error"])
async def test_response_limits_and_types(mode: str) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        headers = {"content-type": "text/html"}
        if mode == "content_length":
            headers["content-length"] = str(MAX_BYTES + 1)
        elif mode == "compressed":
            headers["content-encoding"] = "gzip"
        elif mode == "not_html":
            headers["content-type"] = "application/pdf"
        if mode == "stream":
            return httpx.Response(200, stream=ChunkedBody(), headers=headers)
        return httpx.Response(500 if mode == "error" else 200, content=b"", headers=headers)

    assert not (await read_site("https://salon.example", transport=httpx.MockTransport(handle))).pages


async def test_network_error_returns_empty_site() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("test timeout")

    assert not (await read_site("https://salon.example", transport=httpx.MockTransport(handle))).pages


async def test_redirect_loop_is_bounded() -> None:
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        requests.append(request.url.path)
        return httpx.Response(302, headers={"location": "/loop"})

    assert not (await read_site("https://salon.example", transport=httpx.MockTransport(handle))).pages
    assert len(requests) == 4


@pytest.mark.parametrize(
    "html, expected",
    [
        ('<script src="https://mc.yandex.ru/metrika/tag.js"></script><script>ym(123456,"init",{});</script>', [123456]),
        ('<img src="https://mc.yandex.ru/watch/5678">', [5678]),
        ("<script>var yaCounter9876 = new Ya.Metrika({id:9876});</script>", [9876]),
        ("<script>new Ya.Metrika2({id:8765});</script>", [8765]),
        ('<script src="https://mc.yandex.ru/metrika/tag.js"></script>', []),
        ('<script src="https://mc.yandex.ru/metrika/tag.js"></script><script>ym(0,"init",{});</script>', []),
        ("<p>Номер заказа 87654321</p>", []),
        # Конструктор сайтов: счётчики в JS-конфиге страницы (города → id, ключи не берём).
        (
            '<script>window.config={yandexCityMetric:{"1":81575,"2":22246063,"15":88091110}};</script>',
            [81575, 22246063, 88091110],
        ),
        # Плоский сериализованный массив настроек: только число сразу после id контейнера GTM.
        ('<script>var s=["x","GTM-NTZDZQP4","71565343","3788057","bea560b1722b9860"];</script>', [71565343]),
        # Тот же массив с id контейнера в нижнем регистре — счётчик всё равно находится.
        ('<script>var s=["gtm-ntzdzqp4","71565343"];</script>', [71565343]),
        # ym(..., "init") с коротким числом — не id: даты и индексы не берём.
        ('<script>ym(5,"init",{});</script>', []),
        ('<p>Статья про настройку: ym(5, "init") и всё.</p>', []),
        ('<script>ym(12345,"init",{});</script>', [12345]),
        # Значения в конфиге конструктора — индексы массива, а не id: короткие числа не берём.
        ('<script>{"custom_css":39,"metrics_yandex_metric":9691,"slider":7}</script>', []),
        # Современная прямая вставка tag.js без вызова ym(..., "init") рядом.
        ('<script src="https://mc.yandex.ru/metrika/tag.js?id=123456"></script>', [123456]),
        ('<script src="https://mc.yandex.ru/metrika/tag.js?clickmap=true&id=654321"></script>', [654321]),
        ('<script src="https://mc.yandex.com/metrika/tag.js?id=45678"></script>', [45678]),
        # Строковое значение yandexMetric; пустое — не счётчик.
        ('<script>settings={yandexMetric:"456789"};</script>', [456789]),
        ('<script>settings={yandexMetric:\'456789\'};</script>', [456789]),
        ('<script>settings={yandexMetric:""};</script>', []),
    ],
)
def test_counter_patterns(html: str, expected: list[int]) -> None:
    assert find_metrica_counters(html) == expected


@pytest.mark.parametrize(
    "html, expected",
    [
        (
            '<noscript><iframe src="https://www.googletagmanager.com/ns.html?id=GTM-NTZDZQP4"></iframe></noscript>',
            ["GTM-NTZDZQP4"],
        ),
        (
            "<script>j.src='https://www.googletagmanager.com/gtm.js?id='+i+dl;})(window,document,'script',"
            "'dataLayer','GTM-NTZDZQP4');</script>",
            ["GTM-NTZDZQP4"],
        ),
        (
            '<script src="https://www.googletagmanager.com/gtm.js?l=dataLayer&id=GTM-ABC1234"></script>',
            ["GTM-ABC1234"],
        ),
        ("<p>Сайт без менеджера тегов</p>", []),
        # Id контейнера в конфиге без загрузчика googletagmanager — не контейнер страницы.
        ('<script>gtm:{id:"GTM-WSP53KL",enabled:false}</script>', []),
        # Нижний регистр id — тоже контейнер: сайты пишут и так.
        (
            "<script>j.src='https://www.googletagmanager.com/gtm.js?id='+i;})(window,document,'script','dataLayer',"
            "'gtm-abc1234');</script>",
            ["gtm-abc1234"],
        ),
    ],
)
def test_gtm_container_patterns(html: str, expected: list[str]) -> None:
    assert find_gtm_containers(html) == expected


GTM_SNIPPET = (
    "<script>(function(w,d,s,l,i){w[l]=w[l]||[];w[l].push({'gtm.start':new Date().getTime(),event:'gtm.js'});"
    "var j=d.createElement(s),dl=l!='dataLayer'?'&l='+l:'';j.async=true;"
    "j.src='https://www.googletagmanager.com/gtm.js?id='+i+dl;})(window,document,'script','dataLayer','GTM-NTZDZQP4');"
    "</script>"
)


async def test_gtm_container_is_scanned_for_metrica_counters() -> None:
    """Метрика через custom HTML в контейнере GTM: gtm.js читается один раз и прогоняется через детектор,
    robots.txt чужого хоста не читаем."""
    requests: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:")
        if request.url.host == "www.googletagmanager.com":
            assert request.headers["user-agent"] == "AdAutopilotBot/0.1"
            return httpx.Response(200, text='var a = 1; ym(12345, "init", {});')
        return httpx.Response(200, text=GTM_SNIPPET, headers={"content-type": "text/html"})

    result = await read_site("https://salon.example", transport=httpx.MockTransport(handle))
    assert result.counter_ids == [12345]
    assert result.can_launch
    assert requests.count("https://www.googletagmanager.com/gtm.js?id=GTM-NTZDZQP4") == 1
    assert not any("googletagmanager.com/robots.txt" in url for url in requests)


async def test_duplicate_gtm_containers_are_scanned_once() -> None:
    requests: list[str] = []
    html = GTM_SNIPPET + '<a href="/services">Услуги</a>'

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:")
        if request.url.host == "www.googletagmanager.com":
            return httpx.Response(200, text='ym(12345, "init", {});')
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    result = await read_site("https://salon.example", transport=httpx.MockTransport(handle))
    assert result.counter_ids == [12345]
    assert requests.count("https://www.googletagmanager.com/gtm.js?id=GTM-NTZDZQP4") == 1


async def test_gtm_fetch_errors_are_skipped() -> None:
    """Контейнер GTM недоступен: сайт читается как без счётчика, остальное работает."""
    requests: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:")
        if request.url.host == "www.googletagmanager.com":
            raise httpx.ReadTimeout("gtm timeout")
        return httpx.Response(200, text=GTM_SNIPPET, headers={"content-type": "text/html"})

    result = await read_site("https://salon.example", transport=httpx.MockTransport(handle))
    assert result.pages
    assert result.counter_ids == []
    assert not result.can_launch
    assert requests.count("https://www.googletagmanager.com/gtm.js?id=GTM-NTZDZQP4") == 1


class CountingStream(httpx.AsyncByteStream):
    """Тело ответа, которое считает, сколько раз его реально читали."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.iterations = 0

    async def __aiter__(self):
        self.iterations += 1
        yield self.body


async def test_gtm_not_found_body_is_not_read() -> None:
    """404 от gtm.js: тело не читаем (до лимита 4 МБ впустую), контейнер просто пропускается."""
    requests: list[str] = []
    stream = CountingStream(b"x" * 100_000)

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:")
        if request.url.host == "www.googletagmanager.com":
            return httpx.Response(404, stream=stream)
        return httpx.Response(200, text=GTM_SNIPPET, headers={"content-type": "text/html"})

    result = await read_site("https://salon.example", transport=httpx.MockTransport(handle))
    assert result.pages
    assert result.counter_ids == []
    assert stream.iterations == 0


async def test_only_first_two_gtm_containers_are_scanned() -> None:
    """Кэп контейнеров: враждебная страница с кучей GTM-id не превращает проверку в очередь запросов."""
    requests: list[str] = []
    containers = ("GTM-AAAA001", "GTM-AAAA002", "GTM-AAAA003")
    html = "".join(
        f'<noscript><iframe src="https://www.googletagmanager.com/ns.html?id={container}"></iframe></noscript>'
        for container in containers
    )

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:")
        if request.url.host == "www.googletagmanager.com":
            container = request.url.params["id"]
            counter = 100_000 + containers.index(container) + 1
            return httpx.Response(200, text=f'ym({counter}, "init", {{}});')
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    result = await read_site("https://salon.example", transport=httpx.MockTransport(handle))
    gtm_requests = [url for url in requests if "googletagmanager.com/gtm.js" in url]
    assert len(gtm_requests) == 2
    assert result.counter_ids == [100_001, 100_002]


async def test_gtm_redirect_to_other_host_is_rejected() -> None:
    """gtm.js отвечает редиректом на посторонний хост: hop отвергнут разрешённым списком, контейнер пропущен,
    запрос туда не уходит."""
    requests: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:")
        if request.url.host == "www.googletagmanager.com":
            return httpx.Response(302, headers={"location": "https://evil.example/gtm.js?id=GTM-NTZDZQP4"})
        return httpx.Response(200, text=GTM_SNIPPET, headers={"content-type": "text/html"})

    result = await read_site("https://salon.example", transport=httpx.MockTransport(handle))
    assert result.pages
    assert result.counter_ids == []
    assert not result.can_launch
    assert not any("evil.example" in url for url in requests)


async def test_gtm_container_on_subpage_is_scanned() -> None:
    """Контейнер встретился только на подстранице: он тоже досканируется."""
    requests: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:")
        if request.url.host == "www.googletagmanager.com":
            return httpx.Response(200, text='ym(77777, "init", {});')
        if request.url.path == "/services":
            return httpx.Response(200, text=GTM_SNIPPET, headers={"content-type": "text/html"})
        return httpx.Response(
            200,
            text='<a href="/services">Услуги</a><p>Салон красоты в Самаре.</p>',
            headers={"content-type": "text/html"},
        )

    result = await read_site("https://salon.example", transport=httpx.MockTransport(handle))
    assert result.counter_ids == [77777]
    assert result.can_launch


async def test_lowercase_gtm_container_is_scanned() -> None:
    """Id контейнера в нижнем регистре: детектится и сканируется (GTM принимает любой регистр)."""
    requests: list[str] = []
    snippet = GTM_SNIPPET.replace("GTM-NTZDZQP4", "gtm-ntzdzqp4")

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:")
        if request.url.host == "www.googletagmanager.com":
            return httpx.Response(200, text='ym(88888, "init", {});')
        return httpx.Response(200, text=snippet, headers={"content-type": "text/html"})

    result = await read_site("https://salon.example", transport=httpx.MockTransport(handle))
    assert result.counter_ids == [88888]
    assert "https://www.googletagmanager.com/gtm.js?id=gtm-ntzdzqp4" in requests


async def test_constructor_page_up_to_four_megabytes_is_read() -> None:
    """Страница конструктора сайтов больше старого лимита 1,5 МБ, но в новом 4 МБ: читается, счётчик из
    JS-конфига найден, запуск не блокируется."""
    block = "<p>Меню доставки еды, роллы и пицца.</p>"
    html = (
        '<script>window.config={yandexCityMetric:{"1":81575,"2":22246063}};</script>'
        + block * 40_000
        + "<p>Самара, ул. Примерная, д. 1</p>"
    ).encode()

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:")
        return httpx.Response(200, content=html, headers={"content-type": "text/html"})

    assert 1_500_000 < len(html) < MAX_BYTES
    result = await read_site("https://salon.example", transport=httpx.MockTransport(handle))
    assert len(result.pages) == 1
    assert result.counter_ids == [81575, 22246063]
    assert result.can_launch


def test_html_boundaries_entities_and_hidden_content() -> None:
    page = parse_html(
        "https://salon.example/",
        """<html><head><title>Не цитата</title></head><body>
<p>Маникюр&nbsp;— <b>1500</b> ₽.</p><p>Самара, Полевая 7.</p>
<style>Бесплатный педикюр</style><template>Выдуманный адрес</template>
<a href="TEL:88461234567">Позвонить</a><a href="MAILTO:HELLO@EXAMPLE.ORG">Написать</a>
</body></html>""",
    )
    assert "Маникюр — 1500 ₽.\nСамара, Полевая 7." in page.text
    assert "Не цитата" not in page.text
    assert "Бесплатный" not in page.text
    assert "Выдуманный" not in page.text
    assert page.phones == ["+78461234567"]
    assert page.emails == ["hello@example.org"]


@pytest.mark.parametrize(
    "content, content_type",
    [
        ("Контакты".encode(), "text/html; charset=utf-8"),
        ("Контакты".encode("cp1251"), "text/html; charset=windows-1251"),
        ("Контакты".encode("cp1251"), "text/html"),
        ("Контакты".encode(), "text/html; charset=unknown"),
    ],
)
def test_decode(content: bytes, content_type: str) -> None:
    assert decode(content, content_type) == "Контакты"


def test_decode_meta_charset() -> None:
    html = '<meta charset="windows-1251">Контакты'
    assert decode(html.encode("cp1251"), "text/html") == html


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["10.0.0.1"], ["::1"], ["93.184.216.34", "192.168.1.1"], []])
async def test_dns_rejects_private_or_mixed_addresses(monkeypatch: pytest.MonkeyPatch, addresses: list[str]) -> None:
    lookup = AsyncMock(return_value=[(None, None, None, None, (address, 443)) for address in addresses])
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", lookup)
    assert await resolve_public("salon.example", 443) is None
    backend = PublicOnlyBackend()
    connect = AsyncMock()
    monkeypatch.setattr(backend.backend, "connect_tcp", connect)
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("salon.example", 443)
    connect.assert_not_called()


async def test_public_dns_pins_one_address_and_does_not_resolve_again(monkeypatch: pytest.MonkeyPatch) -> None:
    lookup = AsyncMock(
        side_effect=[
            [(None, None, None, None, ("93.184.216.34", 443)), (None, None, None, None, ("93.184.216.35", 443))],
            [(None, None, None, None, ("127.0.0.1", 443))],
        ]
    )
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", lookup)
    backend = PublicOnlyBackend()
    connect = AsyncMock()
    monkeypatch.setattr(backend.backend, "connect_tcp", connect)
    await backend.connect_tcp("salon.example", 443)
    await backend.connect_tcp("salon.example", 80)
    lookup.assert_awaited_once()
    assert [call.args[:2] for call in connect.await_args_list] == [("93.184.216.34", 443), ("93.184.216.34", 80)]


async def test_backend_checks_port_before_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    lookup = AsyncMock()
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", lookup)
    with pytest.raises(httpcore.ConnectError):
        await PublicOnlyBackend().connect_tcp("salon.example", 8080)
    lookup.assert_not_called()
    assert await resolve_public("salon.example", 8080) is None
    lookup.assert_not_called()


async def test_resolver_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(side_effect=OSError("not found")))
    assert await resolve_public("salon.example", 443) is None


async def test_default_transport_has_public_only_backend() -> None:
    transport = PublicOnlyTransport()
    assert isinstance(transport._pool._network_backend, PublicOnlyBackend)
    await transport.aclose()


@pytest.mark.parametrize("address", ["127.0.0.1", "192.168.1.1", "::1", "fc00::1", "not-an-ip", "224.0.0.1"])
def test_non_public_addresses(address: str) -> None:
    assert not is_public_ip(address)


class MemoryStream(httpcore.AsyncNetworkStream):
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.request = b""
        self.tls_hosts: list[str | None] = []

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        chunk, self.response = self.response[:max_bytes], self.response[max_bytes:]
        return chunk

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.request += buffer

    async def aclose(self) -> None:
        pass

    async def start_tls(
        self, ssl_context: ssl.SSLContext, server_hostname: str | None = None, timeout: float | None = None
    ) -> httpcore.AsyncNetworkStream:
        assert ssl_context.verify_mode == ssl.CERT_REQUIRED
        assert ssl_context.check_hostname
        self.tls_hosts.append(server_hostname)
        return self

    def get_extra_info(self, info: str) -> object:
        return None


async def test_default_http_stack_rejects_redirect_to_private_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    robots = MemoryStream(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
    home = MemoryStream(
        b"HTTP/1.1 302 Found\r\nLocation: https://www.salon.example/\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    )
    connect = AsyncMock(side_effect=[robots, home])
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    lookup = AsyncMock(
        side_effect=[
            [(None, None, None, None, ("93.184.216.34", 443))],
            [(None, None, None, None, ("127.0.0.1", 443))],
        ]
    )
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", lookup)

    result = await read_site("https://salon.example")

    assert result.pages == []
    assert [call.args[:2] for call in lookup.await_args_list] == [
        ("salon.example", 443),
        ("www.salon.example", 443),
    ]
    assert [call.args[:2] for call in connect.await_args_list] == [("93.184.216.34", 443)] * 2
    assert robots.request.startswith(b"GET /robots.txt HTTP/1.1\r\nHost: salon.example")
    assert home.request.startswith(b"GET / HTTP/1.1\r\nHost: salon.example")
    assert robots.tls_hosts == home.tls_hosts == ["salon.example"]


@pytest.mark.parametrize(
    "rule, path, expected",
    [
        ("/private", "/private/secret", True),
        ("/private$", "/private/secret", False),
        ("/private$", "/private", True),
        ("/*/secret$", "/a/b/secret", True),
        ("/*/secret$", "/a/b/secret/more", False),
        ("/a*b*c", "/a1b2c3", True),
        ("/a*b*c", "/a1c2b", False),
        ("*a" * 100 + "b", "a" * 1000, False),
    ],
)
def test_robots_matching(rule: str, path: str, expected: bool) -> None:
    assert rule_matches(rule, path) == expected


def test_malformed_link_does_not_discard_page() -> None:
    page = parse_html("https://salon.example", '<p>Студия маникюра</p><a href="http://[invalid">Услуги</a>')
    assert "Студия маникюра" in page.text
    assert page.links == []


def test_international_phone_keeps_explicit_country_code() -> None:
    page = parse_html("https://salon.example", '<a href="tel:+3312345678">Телефон</a>')
    assert page.phones == ["+3312345678"]


def test_excessively_long_counter_does_not_discard_page() -> None:
    assert find_metrica_counters('<img src="https://mc.yandex.ru/watch/' + "1" * 5000 + '">') == []
