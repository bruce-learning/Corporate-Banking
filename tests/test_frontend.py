async def test_frontend_shell_and_assets(runtime):
    page = await runtime.client.get("/")
    assert page.status_code == 200
    assert "EchoMind 银行对公智能客服" in page.text
    assert "id=\"loginForm\"" in page.text and "id=\"composer\"" in page.text
    css = await runtime.client.get("/static/styles.css")
    script = await runtime.client.get("/static/app.js")
    assert css.status_code == script.status_code == 200
    assert "api/v1/chat" in script.text and "api/v1/memories" in script.text
