"use client";

import { matchRoute } from "@/lib/routes";
import { VIEWS } from "@/lib/views";

export default function ConsoleView({ path }: { path: string }) {
  const route = matchRoute(path);
  if (!route) return null;
  const View = VIEWS[route.id];
  return <View />;
}
