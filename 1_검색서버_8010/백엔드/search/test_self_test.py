from search import app, engine


def test_engine_self_test():
    engine.self_test()


def test_app_self_test():
    app.self_test()
