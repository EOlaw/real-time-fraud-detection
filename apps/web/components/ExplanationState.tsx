import type { ApiError } from "@/lib/api";
import { ErrorState } from "./States";

/** Explanation states are explicit: degraded and model-mismatch are expected outcomes, not failures. */
export function ExplanationState({ error }: { error: ApiError }) {
  if (error.code === "model_mismatch")
    return (
      <p className="notice" role="status">
        Model mismatch: this assessment was scored by a model version that this API instance is not serving, so its
        explanation cannot be reproduced. Its stored probability and decision are unchanged.
      </p>
    );
  if (error.code === "not_explainable")
    return <p className="muted">Degraded assessment: there is no model score to explain.</p>;
  return <ErrorState error={error} what="Explanation" />;
}
