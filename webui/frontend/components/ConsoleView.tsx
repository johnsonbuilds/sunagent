"use client";

import { matchRoute } from "@/lib/routes";
import { VIEWS } from "@/lib/views";

/** Renders the view registered for a statically generated page path. */
export default function ConsoleView({ path }: { path: string }) {
  const route = matchRoute(path);
  if (!route) return null;
  const View = VIEWS[route.id];
  return <View />;
}
