@AGENTS.md

# Notes for Claude Code

`AGENTS.md`, imported above, is the contract. Two more things about how a Claude session commits:

- **Commit messages go in a file, through `git commit -F`.** Backticks in `git commit -m "..."` are shell command
  substitution.
- **End every commit message with a `Co-Authored-By:` trailer** naming the model, as the log already does.
