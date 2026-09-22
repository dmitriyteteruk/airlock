"""Тесты счётчика бульков."""

import logging

logging.getLogger("airlock").setLevel(logging.CRITICAL)
logging.getLogger("airlock").addHandler(logging.NullHandler())
