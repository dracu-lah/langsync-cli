# Security policy

## Supported versions

Only the latest version gets fixes. Run `langsync --version`, and reinstall with
`curl -sSL langsync.nevil.dev | bash` to check the problem still happens on the newest one.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use the **Report a vulnerability**
button on the [Security tab](https://github.com/dracu-lah/langsync-cli/security/advisories/new).

Include the version, what an attacker could do, and the steps to reproduce it. You will get a reply
within a week, and the fix can credit you if you like.

## Scope

LangSync runs on your machine and sends the strings it translates to Google Translate through
[deep-translator](https://github.com/nidhaloff/deep-translator). Problems in those services belong
upstream, though a heads up is welcome when LangSync is affected. The installer at
`langsync.nevil.dev` serves `install.sh` from this repository unchanged.
