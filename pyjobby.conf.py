# Sample Pyjobby Configuration

db_params = {
    "database": "pyjobby_test",
    "user": "pyjobby_test",
    "password": "pyjobby_test_password",
    "host": "localhost",
    "port": 5432,
}

web_listen = {
    "sites": [{"host": "127.0.0.1", "port": 8080}],
    "paths": set(),
}
