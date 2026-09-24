import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "ATLAS · KAG multidimensional",
  description:
    "Grafo de conocimiento de seis dimensiones sobre PostgreSQL Flexible con Apache AGE y pgvector.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="es">
      <body>{children}</body>
    </html>
  );
}
