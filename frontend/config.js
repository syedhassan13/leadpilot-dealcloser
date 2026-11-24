// Where the LeadPilot API lives. Fixed at build time on purpose: the page never takes the API address
// from the URL or from storage, so a crafted link can't send customers or dealer logins to another server
// (pre-launch audit 5.5).
// - Served by the API itself (/ui): same origin is used.
// - Deployed on Vercel: the Render URL below.
const DEPLOYED_API = "https://leadpilot-api-hhys.onrender.com";

window.LEADPILOT_API = (location.hostname === "localhost" || location.hostname === "127.0.0.1")
  ? location.origin
  : DEPLOYED_API;
