export async function register() {
    if (process.env.NEXT_RUNTIME !== "nodejs") return;
    const { startFrontPageWarmer } = await import("./lib/warm");
    startFrontPageWarmer();
}
