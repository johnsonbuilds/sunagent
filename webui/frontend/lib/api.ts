/** Thin wrapper over fetch with the error handling the UI expects. */
export async function api<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const config: RequestInit = {
    headers: { "Content-Type": "application/json" },
    ...options,
  };

  const response = await fetch(path, config);
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body && body.detail) detail = body.detail;
    } catch {
      /* the response was not JSON */
    }
    throw new Error(detail);
  }
  if (response.status === 204) return null as T;
  return (await response.json()) as T;
}
