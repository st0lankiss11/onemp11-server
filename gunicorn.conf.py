def post_fork(server, worker):
    """Called after worker process forks — start news thread here only"""
    from main import start_news_thread
    start_news_thread()
