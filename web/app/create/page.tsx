import { redirect } from "next/navigation";

/**
 * ``/create`` is deprecated — the create-job flow lives on ``/`` with onboarding.
 */
export default function CreateRedirect() {
  redirect("/");
}
