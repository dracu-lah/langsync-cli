// Cloudflare Pages advanced mode. `curl -sSL langsync.pages.dev | bash` gets the
// installer straight from the repo's main branch; browsers get the static site.
const INSTALLER = "https://raw.githubusercontent.com/dracu-lah/langsync-cli/main/install.sh";
const CLI = /\b(curl|wget|fetch|httpie|powershell|python-requests|go-http-client)\b/i;

export default {
  async fetch(request, env) {
    const { pathname } = new URL(request.url);
    const ua = request.headers.get("user-agent") || "";
    if (pathname === "/install.sh" || pathname === "/install" || (pathname === "/" && CLI.test(ua))) {
      const upstream = await fetch(INSTALLER);
      if (!upstream.ok) return new Response("echo 'Could not fetch the LangSync installer. Try again in a minute.' >&2; exit 1\n", { status: 502, headers: { "content-type": "text/plain; charset=utf-8" } });
      return new Response(upstream.body, {
        headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "public, max-age=300" },
      });
    }
    return env.ASSETS.fetch(request);
  },
};
