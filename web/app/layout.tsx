import type { Metadata } from "next";
import { Inter, IBM_Plex_Mono } from "next/font/google";
import "./globals.css";

const inter = Inter({
  subsets: ["latin", "cyrillic"],
  display: "swap",
  variable: "--font-inter",
  weight: ["400", "500", "600", "700"],
});

// IBM Plex Mono для табличных данных (зарплаты, даты, %, ID).
// Tabular numerals — все цифры одинаковой ширины, столбцы чисел не «прыгают»
// при фильтрации/обновлении. См. .tmp/research/dashboard-km-output.md §6.
const plexMono = IBM_Plex_Mono({
  subsets: ["latin", "cyrillic"],
  display: "swap",
  variable: "--font-mono",
  weight: ["400", "500"],
});

export const metadata = {
  title: "VacancyRadar",
  description: "Live dashboard of the Russian IT vacancy market",
} satisfies Metadata;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ru">
      <body className={`${inter.variable} ${plexMono.variable}`}>{children}</body>
    </html>
  );
}
