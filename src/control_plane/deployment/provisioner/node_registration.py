"""Load optional on-prem registration without making cloud startup depend on it."""

import copy
import logging
import os

from .node_config import private_file
from .spec import require

logger = logging.getLogger(__name__)


def configure(service, executor, config, work, infrastructure):
    store = None
    service.onprem_error = None
    try:
        candidate = copy.deepcopy(config)
        registration = work / "onprem-registration" / "bundle.json"
        if os.path.lexists(registration):
            from .node_bundle import install

            require(
                not any(p.is_symlink() for p in registration.parents),
                "invalid on-prem registration directory",
            )
            candidate = install(candidate, private_file(str(registration)))
        cloud = candidate.get("clouds", {}).get("onprem", {})
        require(type(cloud) is dict, "invalid on-prem configuration")
        nodes = cloud.get("nodes", {})
        require(type(nodes) is dict, "invalid on-prem nodes")
        for node in nodes.values():
            require(
                type(node) is dict
                and type(node.get("orgs")) is list
                and node["orgs"]
                and all(isinstance(org, str) for org in node["orgs"]),
                "invalid on-prem node ownership",
            )
        preparation = None
        if settings := candidate.get("onprem"):
            from .drivers.ansible import AnsibleDriver
            from .inventory import Inventory, request
            from .onprem import OnPremService
            from .onprem_store import OnPremStore

            inventory = Inventory(
                settings["catalogFile"],
                settings["secretRoot"],
                allow_local=settings.get("allowLocal", False),
            )
            for node in nodes.values():
                if node.get("preparation"):
                    for org in node["orgs"]:
                        inventory.resolve(
                            request({**node["preparation"], "org": org, "driver": "ansible"})
                        )
            driver = AnsibleDriver(infrastructure, work / "ansible", inventory)
            store = OnPremStore(work / "provisioner.db")
            preparation = OnPremService(
                store, inventory, driver, service.checker, app_store=service.store
            )
        require(
            preparation is not None or not any(node.get("preparation") for node in nodes.values()),
            "on-prem preparation is not configured",
        )
        service.config = candidate
        service.onprem = preparation
        executor.nodes = nodes
        executor.preparations = preparation
    except Exception:
        # Registration is optional. Do not print exception details containing operator secrets.
        if store is not None:
            store.db.close()
        fallback = copy.deepcopy(config)
        fallback.pop("onprem", None)
        fallback.get("clouds", {}).pop("onprem", None)
        service.config = fallback
        service.onprem = None
        service.onprem_error = "ONPREM_CONFIGURATION_UNAVAILABLE"
        executor.nodes = {}
        executor.preparations = None
        logger.warning(
            "On-prem deployment disabled: check the private registration file and catalog. "
            "Cloud deployment remains enabled."
        )
