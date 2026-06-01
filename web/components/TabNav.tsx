"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const TABS = [
  { href: "/", label: "Вакансии" },
  { href: "/trends", label: "Тренды" },
] as const;

export function TabNav() {
  const pathname = usePathname() ?? "/";
  return (
    <nav className="tab-nav" aria-label="Разделы">
      {TABS.map((tab) => {
        const active =
          tab.href === "/" ? pathname === "/" : pathname.startsWith(tab.href);
        return (
          <Link
            key={tab.href}
            href={tab.href}
            className="tab-nav__item"
            data-active={active}
            aria-current={active ? "page" : undefined}
          >
            {tab.label}
          </Link>
        );
      })}
    </nav>
  );
}
