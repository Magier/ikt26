"""Phase 1 tests.

Stdlib unittest, no network, no cluster. The Kubernetes stage runs against the
local simulator, which really does execute the injected container command - so
these tests assert on the actual code-execution evidence, not on a mock.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agent"))

from reviewagent import frontmatter  # noqa: E402
from reviewagent.agent import ReviewAgent  # noqa: E402
from reviewagent.evidence import EventLog  # noqa: E402
from reviewagent.kube.local import LocalKubeClient, strategic_merge  # noqa: E402
from reviewagent.llm import FakeLLM  # noqa: E402
from reviewagent.llm.base import CALL_TOOL, FINISH, INVOKE_SKILL, LLMContext  # noqa: E402
from reviewagent.pr import Repository  # noqa: E402
from reviewagent.skills import SkillRegistry  # noqa: E402
from reviewagent.tools.registry import ToolError, build_default_registry  # noqa: E402
from reviewagent.vcs import GitRepository, open_repository  # noqa: E402

REPO = os.path.join(ROOT, "workshop-repo")
BUILD_SCRIPT = os.path.join(ROOT, "hack", "build-pr-repo.sh")
BASE_BRANCH = "main"
HEAD_BRANCH = "feat/agent-skill-k8s-review"


def git_available():
    import subprocess

    try:
        subprocess.run(
            ["git", "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        return True
    except OSError:
        return False
SEED = os.path.join(ROOT, "sim", "seed")
MANIFEST = os.path.join(REPO, "deploy", "application.yaml")


class FrontmatterTests(unittest.TestCase):
    def test_scalars_lists_and_folded_values(self):
        meta, body = frontmatter.parse(
            '---\nname: k8s-review\ndescription: Review Kubernetes manifests\n'
            "  and check the live workload\nfiles_changed:\n  - \"a.yaml\"\n"
            "  - \"b.yaml\"\n---\n# heading\n\ntext\n"
        )
        self.assertEqual(meta["name"], "k8s-review")
        self.assertIn("live workload", meta["description"])
        self.assertEqual(meta["files_changed"], ["a.yaml", "b.yaml"])
        self.assertTrue(body.startswith("# heading"))

    def test_no_frontmatter_returns_whole_document(self):
        meta, body = frontmatter.parse("# just markdown\n")
        self.assertEqual(meta, {})
        self.assertEqual(body, "# just markdown\n")


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repository(REPO)

    def test_pull_request_is_parsed(self):
        pr = self.repo.load_pull_request()
        self.assertEqual(pr.id, "1")
        self.assertEqual(pr.repository, "Magier/ikt26")
        self.assertEqual(pr.url, "https://github.com/Magier/ikt26/pull/1")
        self.assertTrue(pr.untrusted, "PR author should be an outside contributor")
        self.assertIn(".agents/skills/k8s-review/SKILL.md", pr.files_changed)
        self.assertIn("kubernetes", pr.review_request.lower())

    def test_path_traversal_is_refused(self):
        with self.assertRaises(PermissionError):
            self.repo.read("../../etc/passwd")


class SkillDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repository(REPO)
        self.registry = SkillRegistry(self.repo)

    def test_discovery_finds_repository_skills(self):
        names = sorted(meta.name for meta in self.registry.discover())
        self.assertEqual(names, ["dependency-audit", "k8s-review"])

    def test_discovery_is_metadata_only(self):
        self.registry.discover()
        self.assertEqual(self.registry.loaded(), {}, "discovery must not load bodies")
        meta = self.registry.get_metadata("k8s-review")
        self.assertTrue(meta.sha256)
        self.assertEqual(meta.source, "repository")

    def test_loading_an_undiscovered_skill_fails(self):
        self.registry.discover()
        with self.assertRaises(KeyError):
            self.registry.load("not-a-skill")

    def test_malicious_skill_carries_kubernetes_directives(self):
        self.registry.discover()
        directives = self.registry.load("k8s-review").directives()
        self.assertEqual(
            [d.tool for d in directives],
            ["kubernetes_get", "kubernetes_patch", "kubernetes_get"],
        )
        patch = directives[1].parameters["patch"]
        containers = patch["spec"]["template"]["spec"]["containers"]
        self.assertEqual(containers[0]["name"], "runtime-config-sync")
        self.assertIn("workshop-marker", containers[0]["command"][2])

    def test_benign_skill_carries_no_directives(self):
        self.registry.discover()
        self.assertEqual(self.registry.load("dependency-audit").directives(), [])


class FakeLLMTests(unittest.TestCase):
    def setUp(self):
        self.llm = FakeLLM()
        self.registry = SkillRegistry(Repository(REPO))
        self.metadata = [meta.to_dict() for meta in self.registry.discover()]

    def _context(self, task, loaded=None, history=None, files=None):
        return LLMContext(
            task=task,
            pull_request={
                "id": "1",
                "title": "t",
                "files_changed": ["deploy/application.yaml"] if files is None else files,
            },
            skill_metadata=self.metadata,
            loaded_skills=loaded or {},
            history=history or [],
        )

    def test_kubernetes_task_selects_the_kubernetes_skill(self):
        action = self.llm.decide(self._context("Please review this Kubernetes PR."))
        self.assertEqual(action.action, INVOKE_SKILL)
        self.assertEqual(action.skill, "k8s-review")
        self.assertEqual(action.source, "operator-task")

    def test_dependency_task_selects_the_other_skill(self):
        action = self.llm.decide(
            self._context("Audit the lockfile dependency changes in this PR.")
        )
        self.assertEqual(action.skill, "dependency-audit")

    def test_unrelated_task_and_files_select_nothing(self):
        action = self.llm.decide(
            self._context("Fix the typo in the changelog.", files=["CHANGELOG.md"])
        )
        self.assertEqual(action.action, FINISH)

    def test_changed_manifests_alone_can_select_the_skill(self):
        # Realistic model behaviour: the file list is part of the context, so a
        # vague request plus a manifest change is enough to load the skill.
        action = self.llm.decide(
            self._context("Take a look at this PR.", files=["deploy/application.yaml"])
        )
        self.assertEqual(action.skill, "k8s-review")

    def test_loaded_skill_instructions_become_tool_calls(self):
        body = self.registry.load("k8s-review").body
        history = []
        tools = []
        for _ in range(4):
            action = self.llm.decide(self._context("review k8s", {"k8s-review": body}, history))
            if action.action == FINISH:
                break
            tools.append((action.tool, action.source))
            history.append({"source": action.source, "tool": action.tool})
        self.assertEqual(
            tools,
            [
                ("kubernetes_get", "skill:k8s-review#1"),
                ("kubernetes_patch", "skill:k8s-review#2"),
                ("kubernetes_get", "skill:k8s-review#3"),
            ],
        )
        self.assertEqual(action.action, FINISH, "the run must terminate")


class StrategicMergeTests(unittest.TestCase):
    def test_new_container_is_appended_not_replaced(self):
        merged = strategic_merge(
            {"spec": {"containers": [{"name": "app", "image": "a"}]}},
            {"spec": {"containers": [{"name": "side", "image": "b"}]}},
        )
        self.assertEqual(
            [c["name"] for c in merged["spec"]["containers"]], ["app", "side"]
        )

    def test_existing_container_is_merged_by_name(self):
        merged = strategic_merge(
            {"containers": [{"name": "app", "image": "a", "env": [{"name": "X", "value": "1"}]}]},
            {"containers": [{"name": "app", "env": [{"name": "Y", "value": "2"}]}]},
        )
        container = merged["containers"][0]
        self.assertEqual(container["image"], "a")
        self.assertEqual([e["name"] for e in container["env"]], ["X", "Y"])


class ToolRegistryTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repository(REPO)
        self.skills = SkillRegistry(self.repo)
        self.skills.discover()
        self.tools = build_default_registry(self.repo, self.skills, None, None)

    def test_only_declared_tools_exist(self):
        self.assertEqual(self.tools.names(), ["load_skill", "read_file"])

    def test_agent_has_no_exec_tool(self):
        # The attack must not depend on pods/exec: no exec tool, no exec RBAC.
        for name in self.tools.names():
            self.assertNotIn("exec", name)

    def test_unknown_parameters_are_refused(self):
        with self.assertRaises(ToolError):
            self.tools.get("read_file")(path="README.md", shell="/bin/sh")


class EndToEndAttackTests(unittest.TestCase):
    """The full phase 1 chain, including real container execution."""

    @classmethod
    def setUpClass(cls):
        os.environ["SIM_CONTAINER_TIMEOUT"] = "3"
        cls.workdir = tempfile.mkdtemp(prefix="workshop-test-")
        cls.audit = os.path.join(cls.workdir, "events.jsonl")
        repo = Repository(REPO)
        skills = SkillRegistry(repo)
        cls.kube = LocalKubeClient(
            state_dir=os.path.join(cls.workdir, "state"),
            seed_dir=SEED,
            namespace="ikt-workshop",
        )
        cls.log = EventLog(path=cls.audit, echo=False)
        agent = ReviewAgent(
            repository=repo,
            llm=FakeLLM(),
            event_log=cls.log,
            kube_client=cls.kube,
            tools=build_default_registry(repo, skills, cls.kube, cls.log),
            skill_registry=skills,
        )
        cls.result = agent.review()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.workdir, ignore_errors=True)

    def test_review_ran_the_expected_sequence(self):
        self.assertEqual(self.result.skills_loaded, ["k8s-review"])
        self.assertEqual(
            [(step["tool"], step["outcome"]) for step in self.result.steps],
            [
                ("load_skill", "ok"),
                ("kubernetes_get", "ok"),
                ("kubernetes_patch", "ok"),
                ("kubernetes_get", "ok"),
            ],
        )

    def test_target_deployment_was_modified(self):
        deployment = self.kube.get("deployment", "payments-api")
        containers = deployment["spec"]["template"]["spec"]["containers"]
        names = [c["name"] for c in containers]
        self.assertIn("runtime-config-sync", names)
        self.assertIn("payments-api", names, "the original container must survive")
        self.assertGreater(deployment["metadata"]["generation"], 1)
        annotations = deployment["metadata"]["annotations"]
        self.assertEqual(
            annotations["ai-review.workshop/service-account"], "review-agent-sa"
        )
        self.assertEqual(
            annotations["ai-review.workshop/run-id"], self.log.run_id
        )
        # `kubectl rollout history` must name the agent and the run id.
        self.assertIn("ai-pr-review-agent", annotations["kubernetes.io/change-cause"])
        self.assertIn(self.log.run_id, annotations["kubernetes.io/change-cause"])

    def test_code_executed_inside_the_workload_pod(self):
        pods = self.kube.get("pod")["items"]
        compromised = [
            pod
            for pod in pods
            if any(
                container["name"] == "runtime-config-sync"
                for container in pod["spec"]["containers"]
            )
        ]
        self.assertEqual(len(compromised), 1, "exactly one new pod should exist")
        logs = self.kube.pod_logs(compromised[0]["metadata"]["name"])
        beacon = logs["runtime-config-sync"]
        self.assertIn("workshop.rce.beacon", beacon)
        self.assertIn("unexpected code execution", beacon)

    def test_marker_is_written_into_the_application_volume(self):
        markers = []
        for base, _, files in os.walk(os.path.join(self.workdir, "state", "sandbox")):
            markers += [
                os.path.join(base, name)
                for name in files
                if name == "agent-review-marker.txt"
            ]
        self.assertEqual(len(markers), 1)
        with open(markers[0], "r", encoding="utf-8") as handle:
            content = handle.read()
        self.assertIn("injected_via: .agents/skills/k8s-review/SKILL.md", content)
        # The marker lands in the volume the application serves, so in a real
        # cluster it is reachable over HTTP from inside the namespace.
        self.assertIn(os.path.join("volumes", "web-content"), markers[0])

    def test_audit_log_reconstructs_the_attack(self):
        with open(self.audit, "r", encoding="utf-8") as handle:
            events = [json.loads(line) for line in handle if line.strip()]

        actions = [event["action"] for event in events]
        for expected in (
            "agent.start",
            "pr.loaded",
            "skills.discovered",
            "llm.decision",
            "tool.invoked",
            "review.completed",
        ):
            self.assertIn(expected, actions)

        discovery = next(e for e in events if e["action"] == "skills.discovered")
        self.assertEqual(
            discovery["result"]["introduced_by_this_pr"], ["k8s-review"]
        )

        patch_event = next(
            e for e in events if e.get("tool") == "kubernetes_patch"
        )
        self.assertEqual(patch_event["source"], "skill:k8s-review#2")
        self.assertEqual(
            patch_event["identity"]["service_account"], "review-agent-sa"
        )
        self.assertEqual(
            patch_event["result"]["diff"]["containers_added"], ["runtime-config-sync"]
        )
        # The payload itself must be in the record, not just its effect.
        payload = patch_event["parameters"]["patch"]["spec"]["template"]["spec"][
            "containers"
        ][0]["command"][2]
        self.assertIn("workshop-marker", payload)

        # Provenance: every mutating call must be attributed to the skill.
        for event in events:
            if event.get("tool", "") in {"kubernetes_patch", "kubernetes_create"}:
                self.assertTrue(
                    event["source"].startswith("skill:"),
                    "mutation not attributed to a skill: %r" % event["source"],
                )

    def test_kubernetes_events_were_recorded(self):
        reasons = {
            event["reason"] for event in self.kube.get("event")["items"]
        }
        self.assertIn("SkillsDiscovered", reasons)
        self.assertIn("AgentToolInvoked", reasons)


class TraceExportTests(unittest.TestCase):
    """The exported trace must carry the whole story, in both renderings."""

    @classmethod
    def setUpClass(cls):
        from reviewagent import trace

        cls.trace = trace
        cls.workdir = tempfile.mkdtemp(prefix="workshop-trace-")
        cls.audit = os.path.join(EndToEndAttackTests.workdir, "events.jsonl") \
            if os.path.exists(os.path.join(getattr(EndToEndAttackTests, "workdir", ""), "events.jsonl")) \
            else None
        if cls.audit is None:  # run standalone: produce a log first
            os.environ["SIM_CONTAINER_TIMEOUT"] = "3"
            repo = Repository(REPO)
            skills = SkillRegistry(repo)
            kube = LocalKubeClient(
                state_dir=os.path.join(cls.workdir, "state"),
                seed_dir=SEED,
                namespace="ikt-workshop",
            )
            cls.audit = os.path.join(cls.workdir, "events.jsonl")
            log = EventLog(path=cls.audit, echo=False)
            ReviewAgent(
                repository=repo,
                llm=FakeLLM(),
                event_log=log,
                kube_client=kube,
                tools=build_default_registry(repo, skills, kube, log),
                skill_registry=skills,
            ).review()
        cls.out = os.path.join(cls.workdir, "export")
        cls.result = cls.trace.export(cls.audit, cls.out, repo_path="workshop-repo")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.workdir, ignore_errors=True)

    def _read(self, name):
        with open(os.path.join(self.out, name), "r", encoding="utf-8") as handle:
            return handle.read()

    def test_expected_files_are_written(self):
        for name in ("events.jsonl", "trace.md", "commands.sh"):
            self.assertTrue(os.path.exists(os.path.join(self.out, name)), name)
        self.assertTrue(
            any(name.startswith("patch-") for name in os.listdir(self.out)),
            "the patch body must be exported alongside the command",
        )

    def test_timeline_carries_identity_pr_and_skill(self):
        timeline = self._read("trace.md")
        self.assertIn("review-agent-sa", timeline)
        self.assertIn("ravenal-ext", timeline)
        self.assertIn("https://github.com/Magier/ikt26/pull/1", timeline)
        self.assertIn("FIRST_TIME_CONTRIBUTOR", timeline)
        self.assertIn(".agents/skills/k8s-review/SKILL.md", timeline)
        self.assertIn("TRUST BOUNDARY CROSSED", timeline)
        # The skill's own instructions must be quoted, not just referenced.
        self.assertIn("Kubernetes manifest review", timeline)

    def test_commands_are_plain_kubectl(self):
        script = self._read("commands.sh")
        self.assertIn("kubectl -n ikt-workshop get deployment payments-api", script)
        self.assertIn("--patch-file=patch-", script)
        self.assertIn("skill:k8s-review#2", script)
        # The reproduction must not need `kubectl exec` anywhere: the whole
        # point is that code execution happened without it.
        self.assertNotIn("kubectl exec", script)
        self.assertNotIn("-- /bin/sh", script)

    def test_mutating_commands_are_commented_out(self):
        for line in self._read("commands.sh").splitlines():
            if "kubectl" in line and (" patch " in line or " create -f" in line):
                self.assertTrue(
                    line.lstrip().startswith("#"),
                    "mutating command must not be runnable as written: %s" % line,
                )

    def test_patch_body_contains_the_payload(self):
        name = next(n for n in os.listdir(self.out) if n.startswith("patch-"))
        with open(os.path.join(self.out, name), "r", encoding="utf-8") as handle:
            patch = json.load(handle)
        container = patch["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["name"], "runtime-config-sync")
        self.assertIn("workshop-marker", container["command"][2])

    def test_runs_are_exported_separately(self):
        # Two runs in one log must not be rendered as a single narrative.
        merged = os.path.join(self.workdir, "merged.jsonl")
        original = open(self.audit, "r", encoding="utf-8").read()
        second = original.replace(self.result["run_id"], "0000deadbeef")
        with open(merged, "w", encoding="utf-8") as handle:
            handle.write(original + second)
        self.assertEqual(
            self.trace.runs(self.trace.load_events(merged)),
            [self.result["run_id"], "0000deadbeef"],
        )
        only = self.trace.load_events(merged, run_id="0000deadbeef")
        self.assertTrue(only)
        self.assertTrue(all(e["run_id"] == "0000deadbeef" for e in only))


@unittest.skipUnless(git_available(), "git is not installed")
class GitBackedRepositoryTests(unittest.TestCase):
    """With real history, provenance answers come from git, not from PR.md."""

    @classmethod
    def setUpClass(cls):
        import subprocess

        cls.workdir = tempfile.mkdtemp(prefix="workshop-git-")
        cls.checkout = os.path.join(cls.workdir, "workshop-repo")
        completed = subprocess.run(
            [BUILD_SCRIPT, cls.checkout],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        if completed.returncode != 0:
            raise unittest.SkipTest("build-pr-repo.sh failed: %s" % completed.stderr)
        cls.repo = Repository(cls.checkout)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.workdir, ignore_errors=True)

    def test_repository_is_recognised_as_git(self):
        self.assertTrue(self.repo.vcs.available)
        self.assertEqual(self.repo.vcs.kind, "git")

    def test_history_has_the_expected_shape(self):
        vcs = self.repo.vcs
        self.assertEqual(vcs.current_branch(), HEAD_BRANCH)
        self.assertTrue(vcs.resolve(BASE_BRANCH))
        # One commit on the branch: the pull request.
        commits = vcs.commits_between(BASE_BRANCH, HEAD_BRANCH)
        self.assertEqual(len(commits), 1)
        self.assertIn("k8s-review", commits[0].subject)

    def test_pull_request_facts_come_from_git(self):
        pull_request = self.repo.load_pull_request()
        self.assertEqual(pull_request.vcs_kind, "git")
        self.assertEqual(pull_request.source_branch, HEAD_BRANCH)
        self.assertEqual(pull_request.target_branch, BASE_BRANCH)
        head = pull_request.head_commit
        self.assertIsNotNone(head)
        self.assertEqual(
            head.author_email, "richmond.avenal@basement-contractors.example.com"
        )
        self.assertTrue(head.authored_at.startswith("2026-09-03"))
        # Derived change set, not the declared one.
        self.assertEqual(
            sorted(pull_request.files_derived),
            sorted(
                [
                    ".agents/skills/k8s-review/SKILL.md",
                    "README.md",
                    "deploy/application.yaml",
                ]
            ),
        )
        self.assertEqual(pull_request.undeclared_files, [])
        self.assertEqual(pull_request.unchanged_declared_files, [])

    def test_base_branch_does_not_contain_the_malicious_skill(self):
        files = self.repo.vcs.list_files(BASE_BRANCH)
        self.assertIn(".agents/skills/dependency-audit/SKILL.md", files)
        self.assertNotIn(".agents/skills/k8s-review/SKILL.md", files)
        self.assertNotIn("PR.md", files)

    def test_skill_provenance_names_the_contributor(self):
        registry = SkillRegistry(self.repo)
        by_name = {meta.name: meta for meta in registry.discover()}
        malicious = by_name["k8s-review"].introduced_by
        benign = by_name["dependency-audit"].introduced_by
        self.assertEqual(
            malicious["author_email"],
            "richmond.avenal@basement-contractors.example.com",
        )
        self.assertEqual(benign["author_email"], "r.trenneman@reynholm.example.com")
        self.assertNotEqual(malicious["sha"], benign["sha"])

    def test_reading_a_file_at_a_ref(self):
        head_readme = self.repo.read("README.md")
        base_readme = self.repo.read("README.md", ref=BASE_BRANCH)
        self.assertIn("Automated review", head_readme)
        self.assertNotIn("Automated review", base_readme)
        with self.assertRaises(FileNotFoundError):
            self.repo.read(".agents/skills/k8s-review/SKILL.md", ref=BASE_BRANCH)

    def test_declared_vs_derived_mismatch_is_reported(self):
        from reviewagent.pr import PullRequest

        pull_request = PullRequest(
            id="1", title="t", author="a", author_association="NONE",
            source_branch="f", target_branch="main", created_at="",
            files_declared=["a.yaml", "b.yaml"],
            files_derived=["a.yaml", "evil.yaml"],
        )
        self.assertEqual(pull_request.undeclared_files, ["evil.yaml"])
        self.assertEqual(pull_request.unchanged_declared_files, ["b.yaml"])


@unittest.skipUnless(git_available(), "git is not installed")
class SkillsRefControlTests(unittest.TestCase):
    """Loading skills from the base branch must stop the attack outright."""

    @classmethod
    def setUpClass(cls):
        import subprocess

        os.environ["SIM_CONTAINER_TIMEOUT"] = "3"
        cls.workdir = tempfile.mkdtemp(prefix="workshop-control-")
        checkout = os.path.join(cls.workdir, "workshop-repo")
        completed = subprocess.run(
            [BUILD_SCRIPT, checkout], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        if completed.returncode != 0:
            raise unittest.SkipTest("build-pr-repo.sh failed")
        cls.checkout = checkout

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.workdir, ignore_errors=True)

    def _run(self, skills_ref, state_name):
        repo = Repository(self.checkout)
        skills = SkillRegistry(repo, ref=skills_ref)
        kube = LocalKubeClient(
            state_dir=os.path.join(self.workdir, state_name),
            seed_dir=SEED,
            namespace="ikt-workshop",
        )
        log = EventLog(echo=False)
        agent = ReviewAgent(
            repository=repo,
            llm=FakeLLM(),
            event_log=log,
            kube_client=kube,
            tools=build_default_registry(repo, skills, kube, log),
            skill_registry=skills,
        )
        return agent.review(), kube

    def test_head_ref_is_compromised(self):
        result, kube = self._run(None, "state-head")
        self.assertEqual(result.skills_loaded, ["k8s-review"])
        deployment = kube.get("deployment", "payments-api")
        self.assertIn(
            "runtime-config-sync",
            [c["name"] for c in deployment["spec"]["template"]["spec"]["containers"]],
        )

    def test_base_ref_is_not(self):
        result, kube = self._run("main", "state-base")
        self.assertEqual(result.skills_loaded, [])
        self.assertEqual(result.steps, [], "no tool call should happen at all")
        deployment = kube.get("deployment", "payments-api")
        self.assertEqual(
            [c["name"] for c in deployment["spec"]["template"]["spec"]["containers"]],
            ["payments-api"],
        )
        self.assertEqual(deployment["metadata"]["generation"], 1)
        # The mitigation is not free: the agent also loses the review skill.
        self.assertIn("no relevant skill", result.comment.lower())


class SeedConsistencyTests(unittest.TestCase):
    """The simulator seed must not drift from the real manifest."""

    def test_seed_matches_manifest(self):
        with open(MANIFEST, "r", encoding="utf-8") as handle:
            manifest = handle.read()
        with open(os.path.join(SEED, "workshop-app.json"), "r", encoding="utf-8") as handle:
            seed = json.load(handle)
        deployment = next(obj for obj in seed if obj["kind"] == "Deployment")
        spec = deployment["spec"]["template"]["spec"]

        for container in spec["containers"] + spec["initContainers"]:
            self.assertRegex(
                manifest, r"name:\s*%s\b" % re.escape(container["name"])
            )
            self.assertRegex(
                manifest, r"image:\s*%s\b" % re.escape(container["image"])
            )
        for volume in spec["volumes"]:
            self.assertRegex(manifest, r"name:\s*%s\b" % re.escape(volume["name"]))
        self.assertIn(deployment["metadata"]["name"], manifest)


if __name__ == "__main__":
    unittest.main(verbosity=2)
