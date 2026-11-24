// Where the LeadPilot API lives. Fixed on purpose: the page never takes the API address from the URL or
// from storage, so a crafted link can't redirect customers or dealer logins to another server.
export const DEPLOYED_API = "https://leadpilot-api-hhys.onrender.com";

const local = ["localhost", "127.0.0.1"].includes(location.hostname);
export const API = local ? location.origin : DEPLOYED_API;
