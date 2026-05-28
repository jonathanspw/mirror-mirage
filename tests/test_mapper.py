"""Tests for :mod:`mirror_mirage.mapper`.

The mapper is a pure function: ``FsEvent → list[PurgeTarget]`` where each
binding on the event's owning watch produces one target.
"""

from __future__ import annotations

from pathlib import Path

from mirror_mirage.config import Binding, WatchConfig
from mirror_mirage.filter import EventKind, FsEvent
from mirror_mirage.mapper import Mapper, PurgeTarget


def _ev(kind: EventKind, root: Path, rel: str) -> FsEvent:
    return FsEvent(kind=kind, abs_path=root / rel, watch_root=root)


class TestSingleBinding:
    def test_simple_path_joined_with_prefix(self, watch_factory, mirror_tree: Path) -> None:
        watch = watch_factory()
        mapper = Mapper([watch])
        targets = mapper.map(
            _ev(EventKind.INVALIDATE, mirror_tree, "9/os/x86_64/repodata/repomd.xml")
        )
        assert targets == [
            PurgeTarget(
                cdn_name="fastly_main",
                url="https://repo.almalinux.org/almalinux/9/os/x86_64/repodata/repomd.xml",
            )
        ]

    def test_prefix_without_trailing_slash(self, watch_factory, mirror_tree: Path) -> None:
        watch = watch_factory(
            bindings=[Binding(cdn="fastly_main", url_prefix="https://repo.almalinux.org/almalinux")]
        )
        mapper = Mapper([watch])
        targets = mapper.map(_ev(EventKind.INVALIDATE, mirror_tree, "9/os/x86_64/Packages/x.rpm"))
        assert targets[0].url == "https://repo.almalinux.org/almalinux/9/os/x86_64/Packages/x.rpm"


class TestMultipleBindings:
    def test_fan_out(self, watch_factory, mirror_tree: Path) -> None:
        watch = watch_factory(
            bindings=[
                Binding(cdn="fastly_main", url_prefix="https://repo.almalinux.org/almalinux/"),
                Binding(cdn="cloudfront_us", url_prefix="https://cdn-us.almalinux.org/almalinux/"),
                Binding(cdn="gcp_eu", url_prefix="https://cdn-eu.almalinux.org/almalinux/"),
            ],
        )
        mapper = Mapper([watch])
        targets = mapper.map(
            _ev(EventKind.INVALIDATE, mirror_tree, "9/os/x86_64/repodata/repomd.xml")
        )
        assert len(targets) == 3
        cdn_names = {t.cdn_name for t in targets}
        assert cdn_names == {"fastly_main", "cloudfront_us", "gcp_eu"}
        # All URLs should end with the same relative path.
        for t in targets:
            assert t.url.endswith("/9/os/x86_64/repodata/repomd.xml")


class TestEdgeCases:
    def test_event_for_unknown_watch_returns_empty(
        self, watch_factory, tmp_path: Path, mirror_tree: Path
    ) -> None:
        watch = watch_factory()
        mapper = Mapper([watch])
        # An event whose watch_root doesn't match any configured watch.
        bogus_root = tmp_path / "elsewhere"
        bogus_root.mkdir()
        targets = mapper.map(
            FsEvent(kind=EventKind.INVALIDATE, abs_path=bogus_root / "x", watch_root=bogus_root)
        )
        assert targets == []

    def test_deep_nested_path(self, watch_factory, mirror_tree: Path) -> None:
        watch = watch_factory()
        mapper = Mapper([watch])
        deep = "9/os/x86_64/Packages/a/b/c/d/e/very-deep.rpm"
        targets = mapper.map(_ev(EventKind.INVALIDATE, mirror_tree, deep))
        assert targets[0].url.endswith("/" + deep)

    def test_delete_kind_also_mapped(self, watch_factory, mirror_tree: Path) -> None:
        # Mapper should not care about event kind — both kinds produce targets.
        watch = watch_factory()
        mapper = Mapper([watch])
        targets = mapper.map(_ev(EventKind.DELETE, mirror_tree, "9/os/x86_64/Packages/old.rpm"))
        assert len(targets) == 1

    def test_multiple_watches_routes_to_correct_one(
        self, mirror_tree: Path, tmp_path: Path
    ) -> None:
        other_root = tmp_path / "srv" / "mirror" / "vault"
        other_root.mkdir(parents=True)
        watches = [
            WatchConfig(
                path=mirror_tree,
                bindings=[
                    Binding(cdn="fastly_main", url_prefix="https://repo.almalinux.org/almalinux/")
                ],
            ),
            WatchConfig(
                path=other_root,
                bindings=[Binding(cdn="fastly_main", url_prefix="https://vault.almalinux.org/")],
            ),
        ]
        mapper = Mapper(watches)
        t1 = mapper.map(_ev(EventKind.INVALIDATE, mirror_tree, "9/os/x86_64/x.rpm"))
        t2 = mapper.map(_ev(EventKind.INVALIDATE, other_root, "el7/x.rpm"))
        assert t1[0].url.startswith("https://repo.almalinux.org/almalinux/")
        assert t2[0].url.startswith("https://vault.almalinux.org/")
