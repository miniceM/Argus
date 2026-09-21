import React from "react";
import { QueryClientProvider } from "@tanstack/react-query";
import { queryClient } from "./query-client";

export const AppProviders: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
};
