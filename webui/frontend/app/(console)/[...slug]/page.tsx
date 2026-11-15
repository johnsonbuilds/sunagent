import { notFound } from "next/navigation";

import ConsoleView from "@/components/ConsoleView";
import { matchRoute, ROUTES } from "@/lib/routes";

export const dynamicParams = false;

export function generateStaticParams() {
  return ROUTES.filter((route) => route.path !== "/").map((route) => ({
    slug: route.path.slice(1).split("/"),
  }));
}

export default async function Page({
  params,
}: {
  params: Promise<{ slug: string[] }>;
}) {
  const { slug } = await params;
  const path = `/${slug.join("/")}`;
  if (!matchRoute(path)) notFound();
  return <ConsoleView path={path} />;
}
