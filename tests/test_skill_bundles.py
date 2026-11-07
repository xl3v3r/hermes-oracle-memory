import os
import unittest
from pathlib import Path


class TestSkillBundlesAndCuratedSymlinks(unittest.TestCase):
    def test_curated_symlinks_for_bundle_manifests(self):
        bundles_dir = Path.home() / ".hermes" / "skill-bundles"
        archive_dir = Path.home() / ".hermes" / "archive" / "casemark-skills"
        curated_base = Path.home() / ".hermes" / "skills" / "casemark"

        # Curated bundles are a per-host deployment, not a plugin requirement: hosts that
        # never deployed them (or the CaseMark archive) have nothing to curate. Skip
        # rather than fail, so the suite stays meaningful on every install.
        if not bundles_dir.is_dir():
            self.skipTest(f"curated skill bundles not deployed on this host ({bundles_dir} absent)")
        if not archive_dir.is_dir():
            self.skipTest(f"CaseMark archive not present on this host ({archive_dir} absent)")

        manifest_files = sorted(bundles_dir.glob("*.yaml"))
        self.assertEqual(len(manifest_files), 17, f"Expected 17 manifests, found {len(manifest_files)}")

        casemark_refs = set()
        for mf in manifest_files:
            in_skills = False
            for line in mf.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped == "skills:":
                    in_skills = True
                    continue
                if in_skills:
                    if stripped.startswith("- "):
                        skill_ref = stripped[2:].strip().strip("\"'")
                        if skill_ref.startswith("casemark/"):
                            casemark_refs.add(skill_ref)
                    elif stripped and not stripped.startswith("#"):
                        # Exited skills section
                        in_skills = False

        # Verify exactly 150 unique Casemark skills referenced
        self.assertEqual(len(casemark_refs), 150, f"Expected 150 unique Casemark skills, got {len(casemark_refs)}")

        # Create curated symlinks under ~/.hermes/skills/casemark/<domain>/<skill>
        curated_base.mkdir(parents=True, exist_ok=True)
        created_count = 0
        for ref in sorted(casemark_refs):
            # ref format: casemark/<domain>/<skill_name>
            parts = ref.split("/")
            self.assertEqual(len(parts), 3, f"Unexpected ref format: {ref}")
            domain, skill_name = parts[1], parts[2]

            src_path = archive_dir / domain / skill_name
            self.assertTrue(src_path.is_dir(), f"Archive skill path missing: {src_path}")
            self.assertTrue((src_path / "SKILL.md").is_file(), f"SKILL.md missing in {src_path}")

            target_domain_dir = curated_base / domain
            target_domain_dir.mkdir(parents=True, exist_ok=True)
            link_path = target_domain_dir / skill_name

            if link_path.is_symlink() or link_path.exists():
                if link_path.is_symlink() and link_path.resolve() == src_path.resolve():
                    pass
                else:
                    link_path.unlink()
                    link_path.symlink_to(src_path)
                    created_count += 1
            else:
                link_path.symlink_to(src_path)
                created_count += 1

            self.assertTrue(link_path.is_symlink(), f"Link is not symlink: {link_path}")
            self.assertTrue((link_path / "SKILL.md").is_file(), f"Symlink failed to resolve SKILL.md: {link_path}")

        # Total symlinks under curated_base across all domains should be 150
        all_symlinks = [p for p in curated_base.rglob("*") if p.is_symlink()]
        self.assertEqual(len(all_symlinks), 150)


if __name__ == "__main__":
    unittest.main()
