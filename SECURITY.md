# Security policy

## Supported versions

Security fixes are made on the latest release and on `main`. Older versions are not patched; update to the latest release.

## Reporting a vulnerability

Please do **not** open a public issue for a security problem.

- Preferred: use GitHub's private vulnerability reporting ("Security" tab, "Report a vulnerability") on this repository.
- Alternatively, email **contact@michalsk.pl** with the subject `cad-drawings security`.

Include what you found, how to reproduce it (a minimal synthetic drawing or command line is ideal), the version or commit, and your operating system. Do not send drawings that belong to someone else or contain confidential data.

You can expect an acknowledgement within a few days. This is a volunteer project, so fix times vary; reports of data loss, unexpected process termination, or code execution are handled first.

## What this project does on your machine

Knowing this helps you judge the risk of a report and of using the skill:

- The scripts read drawing files you point them at and write results into a fresh run directory. They are designed never to modify or delete the source file and never to terminate a process they did not start.
- They contain no network code and send no telemetry. (The separate `npx skills` installer has its own anonymous install counter; set `DISABLE_TELEMETRY=1` to turn it off.)
- External programs (ODA File Converter, LibreDWG) are only run when they are installed, and your CAD application only when you agree to it (`--allow-com`), in its own instance that cannot outlive the script.
- Nothing is installed without asking.

## Untrusted drawings

DWG and DXF files are complex formats. Opening a file from an untrusted source with any CAD software, converter or parser carries risk. This project limits what it does with such files (no network access, no execution of embedded code, no following of absolute or UNC external-reference paths), but it relies on third-party libraries and programs to parse them. Keep those up to date, and run the skill on untrusted files in a disposable environment if the source is not trusted.

## Scope

In scope: vulnerabilities in the code in this repository (path handling, file overwrite or deletion, process termination, command construction, handling of crafted input).
Out of scope: vulnerabilities in third-party software the skill can call (CAD applications, ODA File Converter, LibreDWG, ezdxf, pypdfium2); report those to their maintainers.
