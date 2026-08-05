from sharebudget.keyboards import collection_actions, main_menu, webapp_launch


def _buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


def test_active_collection_always_has_onboarding_and_join_buttons():
    collection = {"id": 42, "status": "active"}

    buttons = _buttons(
        collection_actions(
            collection,
            is_member=True,
            is_admin=False,
            start_url="https://t.me/ShakeOnIt_bot?start=collection_42",
            app_url="https://t.me/ShakeOnIt_bot?startapp=collection_42&mode=compact",
        )
    )

    assert any(button.url and "start=collection_42" in button.url for button in buttons)
    assert any(button.url and "startapp=collection_42" in button.url for button in buttons)
    assert any(button.callback_data == "join:42" for button in buttons)


def test_archived_collection_has_no_onboarding_or_join_button():
    collection = {"id": 42, "status": "archived"}

    buttons = _buttons(
        collection_actions(
            collection,
            is_member=True,
            is_admin=False,
            start_url="https://t.me/ShakeOnIt_bot?start=collection_42",
        )
    )

    assert not any(button.url for button in buttons)
    assert not any(button.callback_data == "join:42" for button in buttons)


def test_main_menu_and_launch_button_expose_mini_app():
    menu_labels = [button.text for row in main_menu().keyboard for button in row]
    launch = _buttons(webapp_launch("https://example.com/app"))[0]

    assert "📱 Приложение" in menu_labels
    assert launch.web_app.url == "https://example.com/app"
