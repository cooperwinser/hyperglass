"""Validate router configuration variables."""

# Standard Library
import re
from typing import Any, Set, Dict, List, Tuple, Union, Optional
from pathlib import Path
from ipaddress import IPv4Address, IPv6Address

# Third Party
from pydantic import StrictInt, StrictStr, StrictBool, validator, root_validator

# Project
from hyperglass.log import log
from hyperglass.util import (
    get_driver,
    get_fmt_keys,
    resolve_hostname,
    validate_platform,
)
from hyperglass.state import use_state
from hyperglass.settings import Settings
from hyperglass.constants import SCRAPE_HELPERS, SUPPORTED_STRUCTURED_OUTPUT
from hyperglass.exceptions.private import ConfigError, UnsupportedDevice

# Local
from .ssl import Ssl
from ..main import HyperglassModel, HyperglassModelWithId
from ..util import check_legacy_fields
from .proxy import Proxy
from .params import Params
from ..fields import SupportedDriver
from .network import Network
from ..directive import Directives
from .credential import Credential


class DirectiveOptions(HyperglassModel, extra="ignore"):
    """Per-device directive options."""

    builtins: Union[StrictBool, List[StrictStr]] = True


class Device(HyperglassModelWithId, extra="allow"):
    """Validation model for per-router config in devices.yaml."""

    id: StrictStr
    name: StrictStr
    address: Union[IPv4Address, IPv6Address, StrictStr]
    network: Network
    credential: Credential
    proxy: Optional[Proxy]
    display_name: Optional[StrictStr]
    port: StrictInt = 22
    ssl: Optional[Ssl]
    platform: StrictStr
    directives: Directives
    structured_output: Optional[StrictBool]
    driver: Optional[SupportedDriver]
    attrs: Dict[str, str] = {}

    def __init__(self, **kwargs) -> None:
        """Set the device ID."""
        kwargs = check_legacy_fields("Device", **kwargs)
        _id, values = self._generate_id(kwargs)
        super().__init__(id=_id, **values)
        self._validate_directive_attrs()

    @property
    def _target(self):
        return str(self.address)

    @staticmethod
    def _generate_id(values: Dict) -> Tuple[str, Dict]:
        """Generate device id & handle legacy display_name field."""

        def generate_id(name: str) -> str:
            scrubbed = re.sub(r"[^A-Za-z0-9\_\-\s]", "", name)
            return "_".join(scrubbed.split()).lower()

        name = values.pop("name", None)

        if name is None:
            raise ValueError("name is required.")

        legacy_display_name = values.pop("display_name", None)

        if legacy_display_name is not None:
            log.warning("The 'display_name' field is deprecated. Use the 'name' field instead.")
            device_id = generate_id(legacy_display_name)
            display_name = legacy_display_name
        else:
            device_id = generate_id(name)
            display_name = name

        return device_id, {"name": display_name, "display_name": None, **values}

    def export_api(self) -> Dict[str, Any]:
        """Export API-facing device fields."""
        return {
            "id": self.id,
            "name": self.name,
            "network": self.network.display_name,
        }

    @property
    def directive_commands(self) -> List[str]:
        """Get all commands associated with the device."""
        return [
            command
            for directive in self.directives
            for rule in directive.rules
            for command in rule.commands
        ]

    @property
    def directive_ids(self) -> List[str]:
        """Get all directive IDs associated with the device."""
        return [directive.id for directive in self.directives]

    def has_directives(self, *directive_ids: str) -> bool:
        """Determine if a directive is used on this device."""
        for directive_id in directive_ids:
            if directive_id in self.directive_ids:
                return True
        return False

    def _validate_directive_attrs(self) -> None:

        # Set of all keys except for built-in key `target`.
        keys = {
            key
            for group in [get_fmt_keys(command) for command in self.directive_commands]
            for key in group
            if key != "target"
        }

        attrs = {k: v for k, v in self.attrs.items() if k in keys}

        # Verify all keys in associated commands contain values in device's `attrs`.
        for key in keys:
            if key not in attrs:
                raise ConfigError(
                    "Device '{d}' has a command that references attribute '{a}', but '{a}' is missing from device attributes",
                    d=self.name,
                    a=key,
                )

    @validator("address")
    def validate_address(cls, value, values):
        """Ensure a hostname is resolvable."""

        if not isinstance(value, (IPv4Address, IPv6Address)):
            if not any(resolve_hostname(value)):
                raise ConfigError(
                    "Device '{d}' has an address of '{a}', which is not resolvable.",
                    d=values["name"],
                    a=value,
                )
        return value

    @validator("structured_output", pre=True, always=True)
    def validate_structured_output(cls, value: bool, values: Dict) -> bool:
        """Validate structured output is supported on the device & set a default."""

        if value is True:
            if values["platform"] not in SUPPORTED_STRUCTURED_OUTPUT:
                raise ConfigError(
                    "The 'structured_output' field is set to 'true' on device '{d}' with "
                    + "platform '{p}', which does not support structured output",
                    d=values["name"],
                    p=values["platform"],
                )
            return value
        elif value is None and values["platform"] in SUPPORTED_STRUCTURED_OUTPUT:
            value = True
        else:
            value = False
        return value

    @validator("ssl")
    def validate_ssl(cls, value, values):
        """Set default cert file location if undefined."""

        if value is not None:
            if value.enable and value.cert is None:
                cert_file = Settings.app_path / "certs" / f'{values["name"]}.pem'
                if not cert_file.exists():
                    log.warning("No certificate found for device {d}", d=values["name"])
                    cert_file.touch()
                value.cert = cert_file
        return value

    @root_validator(pre=True)
    def validate_device(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        """Validate & rewrite device platform, set default `directives`."""

        platform = values.get("platform")
        if platform is None:
            # Ensure device platform is defined.
            raise ConfigError(
                "Device '{device}' is missing a 'platform' (Network Operating System) property",
                device={values["name"]},
            )

        if platform in SCRAPE_HELPERS.keys():
            # Rewrite NOS to helper value if needed.
            platform = SCRAPE_HELPERS[platform]

        # Verify device platform is supported by hyperglass.
        supported, _ = validate_platform(platform)
        if not supported:
            raise UnsupportedDevice(platform)

        values["platform"] = platform

        directives = use_state("directives")

        directive_ids = values.get("directives", [])

        # Directive options
        directive_options = DirectiveOptions(
            **{
                k: v
                for statement in directive_ids
                if isinstance(statement, Dict)
                for k, v in statement.items()
            }
        )

        # String directive IDs, excluding builtins and options.
        directive_ids = [
            statement
            for statement in directive_ids
            if isinstance(statement, str) and not statement.startswith("__")
        ]
        # Directives matching provided IDs.
        device_directives = directives.filter_by_ids(*directive_ids)
        # Matching built-in directives for this device's platform.
        builtins = directives.device_builtins(platform=platform)

        if directive_options.builtins is True:
            # Add all builtins.
            device_directives += builtins
        elif isinstance(directive_options.builtins, List):
            # If the user provides a list of builtin directives to include, add only those.
            device_directives += builtins.matching(*directive_options.builtins)

        values["directives"] = device_directives
        return values

    @validator("driver")
    def validate_driver(cls, value: Optional[str], values: Dict) -> Dict:
        """Set the correct driver and override if supported."""
        return get_driver(values["platform"], value)


class Devices(HyperglassModel, extra="allow"):
    """Validation model for device configurations."""

    ids: List[StrictStr] = []
    hostnames: List[StrictStr] = []
    objects: List[Device] = []

    def __init__(self, input_params: List[Dict]) -> None:
        """Import loaded YAML, initialize per-network definitions.

        Remove unsupported characters from device names, dynamically
        set attributes for the devices class. Builds lists of common
        attributes for easy access in other modules.
        """
        objects = set()
        hostnames = set()
        ids = set()

        init_kwargs = {}

        for definition in input_params:
            # Validate each router config against Router() model/schema
            device = Device(**definition)

            # Add router-level attributes (assumed to be unique) to
            # class lists, e.g. so all hostnames can be accessed as a
            # list with `devices.hostnames`, same for all router
            # classes, for when iteration over all routers is required.
            hostnames.add(device.name)
            ids.add(device.id)
            objects.add(device)

        # Convert the de-duplicated sets to a standard list, add lists
        # as class attributes. Sort router list by router name attribute
        init_kwargs["ids"] = list(ids)
        init_kwargs["hostnames"] = list(hostnames)
        init_kwargs["objects"] = sorted(objects, key=lambda x: x.name)

        super().__init__(**init_kwargs)

    def __getitem__(self, accessor: str) -> Device:
        """Get a device by its name."""
        for device in self.objects:
            if device.id == accessor:
                return device
            elif device.name == accessor:
                return device

        raise AttributeError(f"No device named '{accessor}'")

    def export_api(self) -> List[Dict[str, Any]]:
        """Export API-facing device fields."""
        return [d.export_api() for d in self.objects]

    def networks(self, params: Params) -> List[Dict[str, Any]]:
        """Group devices by network."""
        names = {device.network.display_name for device in self.objects}
        return [
            {
                "display_name": name,
                "locations": [
                    {
                        "id": device.id,
                        "name": device.name,
                        "network": device.network.display_name,
                        "directives": [d.frontend(params) for d in device.directives],
                    }
                    for device in self.objects
                    if device.network.display_name == name
                ],
            }
            for name in names
        ]

    def directive_plugins(self) -> Dict[Path, Tuple[StrictStr]]:
        """Get a mapping of plugin paths to associated directive IDs."""
        result: Dict[Path, Set[StrictStr]] = {}
        # Unique set of all directives.
        directives = {directive for device in self.objects for directive in device.directives}
        # Unique set of all plugin file names.
        plugin_names = {plugin for directive in directives for plugin in directive.plugins}

        for directive in directives:
            # Convert each plugin file name to a `Path` object.
            for plugin in (Path(p) for p in directive.plugins if p in plugin_names):
                if plugin not in result:
                    result[plugin] = set()
                result[plugin].add(directive.id)
        # Convert the directive set to a tuple.
        return {k: tuple(v) for k, v in result.items()}
