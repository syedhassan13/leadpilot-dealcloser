// Where the LeadPilot API lives.
// - Served by the API itself (/ui): leave DEPLOYED_API empty, same origin is used.
// - Deployed on Vercel: put your Render URL here, e.g. "https://leadpilot-api-hhys.onrender.com".
// You can also override it per browser with ?api=https://... in the URL.
const DEPLOYED_API = "https://leadpilot-api-hhys.onrender.com";

window.LEADPILOT_API = (() => {
  const fromUrl = new URLSearchParams(location.search).get("api");
  if (fromUrl) { try { localStorage.setItem("lp_api", fromUrl); } catch (e) {} }
  let saved = null;
  try { saved = localStorage.getItem("lp_api"); } catch (e) {}
  return (fromUrl || saved || DEPLOYED_API || location.origin).replace(/\/$/, "");
})();
