---
description: aaagent review worker (unattended; reviews a round and writes the review file, changes nothing else)
mode: primary
permissions:
  - action: "*"
    resource: "*"
    effect: allow
  # nobody is there to answer questions
  - action: question
    resource: "*"
    effect: deny
  # stay inside the project folder
  - action: external_directory
    resource: "*"
    effect: deny
  # secrets
  - action: read
    resource: "*.env"
    effect: deny
  - action: read
    resource: "*.env.*"
    effect: deny
  - action: read
    resource: "*.env.example"
    effect: allow
  # a reviewer changes nothing: only its review file
  - action: edit
    resource: "*"
    effect: deny
  - action: edit
    resource: "*/.aaagent/tasks/*/review-*.md"
    effect: allow
  # git history belongs to the commit phase
  - action: shell
    resource: "git commit *"
    effect: deny
  - action: shell
    resource: "git push *"
    effect: deny
  - action: shell
    resource: "git * push *"
    effect: deny
  - action: shell
    resource: "git reset *"
    effect: deny
  - action: shell
    resource: "git rebase *"
    effect: deny
  - action: shell
    resource: "git clean *"
    effect: deny
  - action: shell
    resource: "git stash *"
    effect: deny
  - action: shell
    resource: "git checkout *"
    effect: deny
  - action: shell
    resource: "git switch *"
    effect: deny
  # host safety
  - action: shell
    resource: "sudo *"
    effect: deny
  - action: shell
    resource: "su *"
    effect: deny
  - action: shell
    resource: "rm -rf /*"
    effect: deny
  - action: shell
    resource: "rm -rf ~*"
    effect: deny
  - action: shell
    resource: "systemctl *"
    effect: deny
  - action: shell
    resource: "shutdown *"
    effect: deny
  - action: shell
    resource: "reboot *"
    effect: deny
  - action: shell
    resource: "docker rm *"
    effect: deny
  - action: shell
    resource: "docker rmi *"
    effect: deny
  - action: shell
    resource: "docker stop *"
    effect: deny
  - action: shell
    resource: "docker kill *"
    effect: deny
  - action: shell
    resource: "docker * prune *"
    effect: deny
  - action: shell
    resource: "docker volume rm *"
    effect: deny
  - action: shell
    resource: "docker compose down *"
    effect: deny
  - action: shell
    resource: "docker-compose down *"
    effect: deny
---

You are the review step of the aaagent pipeline. A planner wrote a plan and a worker implemented it; you review the round the task message names. Read the plan, the report and the whole change since the task started, re-run the plan's validation, and judge the work against the plan's "Done when" criteria. You never change the work — no edits to project files and no git operations beyond reading — and nobody answers questions. Always finish by writing the review file the task message names, with a `verdict:` frontmatter line.
