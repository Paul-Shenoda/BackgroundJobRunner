import { useEffect, useState } from "react";
import { listLanes } from "../lib/apiClient";
import { useAuth } from "../context/AuthContext";

export function useLanes() {
  const { apiKey } = useAuth();
  const [lanes, setLanes] = useState([]);

  useEffect(() => {
    let cancelled = false;
    listLanes(apiKey).then((result) => {
      if (!cancelled) setLanes(result);
    });
    return () => {
      cancelled = true;
    };
  }, [apiKey]);

  return lanes;
}
