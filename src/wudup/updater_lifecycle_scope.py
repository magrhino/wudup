"""Update-scope helpers for updater lifecycle execution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .command import CommandError
from .compose import ComposeStack
from .updater_matching import (
    RECREATE_STACK_LABEL,
    RECREATE_STACK_LABEL_FORMAT,
    _expand_network_mode_services,
    _label_value_is_true,
    _network_mode_providers,
    _ordered_unique,
    _update_services,
)
from .updater_models import Match, UpdateScope


def runtime_services_for_scope(
    scope: UpdateScope,
) -> tuple[str, ...]:
    if scope.services is not None:
        services = scope.services
    elif scope.stop_services is not None:
        services = tuple(reversed(scope.stop_services))
    else:
        services = ()
    return _ordered_unique(services)


class _UpdateScopeMixin:
    def _update_scope(
        self, stack: ComposeStack, matches: Sequence[Match], *, strict: bool = False,
    ) -> UpdateScope:
        services = _update_services(matches)
        if not strict:
            verified = getattr(self, "verified_update_scopes", {}).get((stack.index, services))
            if verified is not None:
                return verified
        if services is None:
            return UpdateScope(
                services=None,
                pull_services=None,
                stop_services=self._stack_stop_services(stack, strict=strict),
                force_recreate=True,
            )
        network_providers = _network_mode_providers(stack.service_images)
        lifecycle_services, uses_network_provider = _expand_network_mode_services(
            services,
            network_providers,
        )
        missing_providers = self._missing_network_mode_providers(
            stack,
            services,
            network_providers,
            strict=strict,
        )
        if missing_providers:
            lifecycle_services = _ordered_unique((*missing_providers, *lifecycle_services))
            uses_network_provider = True
        stop_services = (
            services
            if missing_providers
            else tuple(reversed(lifecycle_services))
            if uses_network_provider
            else lifecycle_services
        )

        label_cid = self._stack_recreate_label_cid(stack, lifecycle_services, strict=strict)
        if label_cid:
            return UpdateScope(
                services=None,
                pull_services=services,
                stack_reason=(
                    f"selected service scope container {label_cid} has "
                    f"{RECREATE_STACK_LABEL}=true"
                ),
                stop_services=self._stack_stop_services(stack, strict=strict),
                force_recreate=False,
            )
        return UpdateScope(
            services=lifecycle_services,
            pull_services=services,
            stop_services=stop_services,
            up_no_deps=not uses_network_provider,
        )

    def _missing_network_mode_providers(
        self,
        stack: ComposeStack,
        services: Sequence[str],
        providers: Mapping[str, str],
        *,
        strict: bool = False,
    ) -> tuple[str, ...]:
        missing: list[str] = []
        for service in services:
            provider = providers.get(service)
            if not provider or provider in services or provider in missing:
                continue
            lookup = self.compose.ps_quiet_checked if strict else self.compose.ps_quiet
            cids = lookup(
                stack.directory,
                stack.file,
                (provider,),
                project_directory=stack.project_directory,
            )
            if not cids:
                missing.append(provider)
        return tuple(missing)

    def _stack_stop_services(
        self, stack: ComposeStack, *, strict: bool = False,
    ) -> tuple[str, ...] | None:
        try:
            services = self.compose.config_services(
                stack.directory,
                stack.file,
                project_directory=stack.project_directory,
            )
        except CommandError:
            if strict:
                raise
            return None
        if not services:
            if strict:
                raise ValueError("Compose service discovery returned no services")
            return None
        return tuple(reversed(services))

    def _stack_recreate_label_cid(
        self,
        stack: ComposeStack,
        services: Sequence[str],
        *,
        strict: bool = False,
    ) -> str:
        lookup = self.compose.ps_quiet_checked if strict else self.compose.ps_quiet
        inspect = self.docker.inspect if strict else self.docker.try_inspect
        for cid in lookup(
            stack.directory,
            stack.file,
            services,
            project_directory=stack.project_directory,
        ):
            for value in inspect(cid, RECREATE_STACK_LABEL_FORMAT):
                if _label_value_is_true(value):
                    return cid
        return ""


def _stack_level_scope_message(scope: UpdateScope) -> str:
    if scope.stack_reason:
        if scope.pull_services is not None:
            return f"{scope.stack_reason}; using service pull with stack-level recreate"
        return f"{scope.stack_reason}; using stack-level pull/recreate"
    return (
        "Could not map every matched image to a compose service; "
        "using stack-level pull/recreate"
    )
