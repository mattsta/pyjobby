# Configuration defined by:
# https://magicstack.github.io/asyncpg/current/api/index.html#connection
db_params = {
    "database": "dbname",
    "user": "username",
    "password": "",
    "host": "/tmp",  # can be directory for sock or IP/hostname
    "port": "5432",  # optional when using directory
}

# hosts/ports/paths where workers will listen for out-of-queue requests.
# Paths are classes allowed for web use, access in the form of:
# http://127.0.0.1:6661/job/job.image.thumbnails.Thumbnails
# The .web() method of the class will run with web.Request as an argument
# when any query (get/post/websocket—anything) is sent to the endpoint.
# Formatted as:
#   - 'sites' key is a list of kwargs for aiohttp web.TCPSite:
#     - https://github.com/aio-libs/aiohttp/blob/3014db406268ff74cabbee75ca5fbc23ffe0dd1c/aiohttp/web_runner.py#L88https://docs.aiohttp.org/en/stable/web_reference.html#aiohttp.web.run_app
#     - or for UnixSite: https://github.com/aio-libs/aiohttp/blob/3014db406268ff74cabbee75ca5fbc23ffe0dd1c/aiohttp/web_runner.py#L138
#       - NOTE: unix paths will be appended with the worker process number as: path-X, so
#         /tmp/pj.sock will be /tmp/pj.sock-1, /tmp/pj.sock-2, ...
#   - 'paths' key is an iterable of all job classes to make available as endpoints.
web_listen = {
    "sites": [{"host": "127.0.0.1", "port": 6661}, {"path": "/tmp/pj.sock"}],
    "paths": set(["job.image.thumbnails.Thumbnails"]),
}
