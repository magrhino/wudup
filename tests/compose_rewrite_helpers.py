from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wudup.compose import ComposeStack, ServiceImage
from wudup.compose_rewrite import compose_escape_dollars, exact_tags_regex
from wudup.tag_streams import retag_tag_include_regex
from wudup.updater_models import (
    DigestPinUpdate,
    DigestUnpinUpdate,
    TagExclusionUpdate,
)


class ComposeRewriteTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="wud-compose-rewrite.")
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_compose(self, source: str, name: str = "compose.yml") -> Path:
        compose_path = self.root / name
        compose_path.write_text(source, encoding="utf-8")
        return compose_path

    def stack(
        self,
        service_images: tuple[tuple[str, str], ...] = (("app", "repo/app:1.0"),),
    ) -> ComposeStack:
        return ComposeStack(
            index=1,
            directory=self.root,
            file="compose.yml",
            name="stack",
            images=tuple(image for _service, image in service_images),
            service_images=tuple(
                ServiceImage(service, image) for service, image in service_images
            ),
        )

    def tag_exclusion_update(
        self,
        *,
        service: str = "app",
        image: str = "repo/app:1.0",
        image_repo: str = "repo/app",
        tag: str = "2.0",
        scope: str = "service",
    ) -> TagExclusionUpdate:
        return TagExclusionUpdate(
            stack=self.stack(((service, image),)),
            service=service,
            image=image,
            image_repo=image_repo,
            tag=tag,
            source_line=1,
            scope=scope,
        )

    def digest_pin_update(
        self,
        *,
        old_image: str = "repo/app:1.0",
        resolved_tag: str = "2.0",
        planned_digest: str = "sha256:pin",
        services: tuple[str, ...] = ("app",),
    ) -> DigestPinUpdate:
        image_repo = old_image.rsplit(":", 1)[0]
        return DigestPinUpdate(
            old_image=old_image,
            resolved_tag=resolved_tag,
            resolved_image=f"{image_repo}:{resolved_tag}",
            planned_digest=planned_digest,
            final_image=f"{image_repo}@{planned_digest}",
            watch_tag=resolved_tag,
            marker=f"wudup.resolved-tag={resolved_tag}",
            label_key="wud.tag.include",
            label_value=compose_escape_dollars(exact_tags_regex((resolved_tag,))),
            services=services,
        )

    def digest_unpin_update(
        self,
        *,
        old_image: str = "repo/app@sha256:old",
        resolved_tag: str = "latest",
        target_digest: str = "sha256:new",
        services: tuple[str, ...] = ("app",),
    ) -> DigestUnpinUpdate:
        image_repo = old_image.split("@", 1)[0].rsplit(":", 1)[0]
        return DigestUnpinUpdate(
            old_image=old_image,
            resolved_tag=resolved_tag,
            tag_image=f"{image_repo}:{resolved_tag}",
            current_digest="sha256:old",
            target_digest=target_digest,
            watch_tag=resolved_tag,
            marker=f"wudup.resolved-tag={resolved_tag}",
            label_key="wud.tag.include",
            label_value=compose_escape_dollars(retag_tag_include_regex(resolved_tag)),
            services=services,
        )
