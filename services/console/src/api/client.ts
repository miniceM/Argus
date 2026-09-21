import createClient from "openapi-fetch";
import type { paths } from "./schema";

export const api = createClient<paths>({
  baseUrl: typeof window !== "undefined" ? window.location.origin : "",
});
