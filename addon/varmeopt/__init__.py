"""Varmeopt — prisstyret optimering af varmekilder.

Versionen sættes af Dockerfilen ud fra Supervisors ``BUILD_VERSION``, så den
kun vedligeholdes ét sted: ``version:`` i ``config.yaml``. Uden for add-on'en
er den ikke sat.

Den siger hvilket *image* der kører. Kører koden fra ``/data/code`` efter en
selvopdatering, er det ``selfupdate.current()`` der siger hvilken *kode* det
er — og de to kan udmærket pege hvert sit sted.
"""

from __future__ import annotations

import os

VERSION = os.environ.get("VARMEOPT_VERSION", "ukendt")
