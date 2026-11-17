"use client";

import { UserPlus } from "lucide-react";
import { useState, type FormEvent } from "react";
import { api, type Role, type User } from "@/lib/api";
import { errorMessage, relativeDay } from "@/lib/format";
import { useQuery } from "@/lib/use-query";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Field, Input, Select } from "@/components/ui/field";
import { EmptyState, ErrorNote, LoadingLines } from "@/components/ui/feedback";
import { PageHeader, Panel } from "@/components/ui/page";
import { Status } from "@/components/ui/status";
import { useToast } from "@/components/ui/toast";

const ROLES: Role[] = ["student", "professor", "admin"];
const ROLE_LABEL: Record<Role, string> = {
  student: "Student",
  professor: "Professor",
  admin: "Admin",
};

function AddUserDialog({
  open,
  onClose,
  onAdded,
}: {
  open: boolean;
  onClose: () => void;
  onAdded: (user: User) => void;
}) {
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("student");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function reset() {
    setDisplayName("");
    setEmail("");
    setRole("student");
    setError(null);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const user = await api.createUser({ displayName, email, role });
      onAdded(user);
      reset();
      onClose();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      open={open}
      onClose={() => {
        reset();
        onClose();
      }}
      title="Add an account"
    >
      <form onSubmit={submit} className="space-y-4" noValidate>
        <Field label="Full name" htmlFor="new-name">
          <Input
            id="new-name"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            autoComplete="off"
            required
          />
        </Field>
        <Field
          label="Email"
          htmlFor="new-email"
          hint="They set their own password the first time they sign in."
        >
          <Input
            id="new-email"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="name@university.edu"
            autoComplete="off"
            required
          />
        </Field>
        <Field label="Role" htmlFor="new-role">
          <Select id="new-role" value={role} onChange={(e) => setRole(e.target.value as Role)}>
            {ROLES.map((r) => (
              <option key={r} value={r}>
                {ROLE_LABEL[r]}
              </option>
            ))}
          </Select>
        </Field>

        {error ? (
          <p role="alert" className="rounded-md bg-crit-wash px-3 py-2 text-sm text-crit-ink">
            {error}
          </p>
        ) : null}

        <div className="flex justify-end gap-2 pt-1">
          <Button
            onClick={() => {
              reset();
              onClose();
            }}
          >
            Cancel
          </Button>
          <Button type="submit" variant="primary" busy={busy}>
            Add account
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

export default function UsersPage() {
  const toast = useToast();
  const { data, error, reload, setData } = useQuery("users", () => api.listUsers());
  const [adding, setAdding] = useState(false);
  const [working, setWorking] = useState<string | null>(null);

  async function patch(user: User, change: Partial<Pick<User, "role" | "status">>, note: string) {
    setWorking(user.id);
    try {
      const updated = await api.updateUser(user.id, change);
      setData((data ?? []).map((u) => (u.id === updated.id ? updated : u)));
      toast(note);
    } catch (reason) {
      toast(errorMessage(reason), "error");
    } finally {
      setWorking(null);
    }
  }

  if (error) return <ErrorNote error={error} onRetry={reload} />;

  return (
    <>
      <PageHeader
        title="Users"
        description="Who can sign in, and what they are allowed to do."
        actions={
          <Button
            variant="primary"
            icon={<UserPlus className="size-4" aria-hidden />}
            onClick={() => setAdding(true)}
          >
            Add an account
          </Button>
        }
      />

      <Panel flush>
        {!data ? (
          <div className="p-5">
            <LoadingLines label="Loading accounts" />
          </div>
        ) : data.length === 0 ? (
          <div className="p-5">
            <EmptyState title="No accounts yet">Add the first one to get started.</EmptyState>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="table-grid w-full">
              <caption className="sr-only">Accounts</caption>
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col">Role</th>
                  <th scope="col">Status</th>
                  <th scope="col">Last active</th>
                  <th scope="col">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {data.map((user) => (
                  <tr key={user.id} className={working === user.id ? "opacity-60" : undefined}>
                    <td>
                      <div className="font-semibold text-ink">{user.displayName}</div>
                      <div className="text-sm break-all text-ink-3">{user.email}</div>
                    </td>
                    <td>
                      <Select
                        aria-label={`Role for ${user.displayName}`}
                        value={user.role}
                        disabled={working === user.id}
                        onChange={(e) =>
                          void patch(
                            user,
                            { role: e.target.value as Role },
                            `${user.displayName} is now a ${ROLE_LABEL[e.target.value as Role].toLowerCase()}.`,
                          )
                        }
                        className="h-8 w-36 text-sm"
                      >
                        {ROLES.map((r) => (
                          <option key={r} value={r}>
                            {ROLE_LABEL[r]}
                          </option>
                        ))}
                      </Select>
                    </td>
                    <td>
                      {user.status === "active" ? (
                        <Status tone="good">Active</Status>
                      ) : (
                        <Status tone="neutral">Disabled</Status>
                      )}
                    </td>
                    <td className="whitespace-nowrap text-ink-2">
                      {user.lastActiveAt ? relativeDay(user.lastActiveAt) : "Never signed in"}
                    </td>
                    <td className="text-right">
                      <Button
                        size="sm"
                        variant={user.status === "active" ? "danger" : "secondary"}
                        disabled={working === user.id}
                        onClick={() =>
                          void patch(
                            user,
                            { status: user.status === "active" ? "disabled" : "active" },
                            user.status === "active"
                              ? `${user.displayName} can no longer sign in.`
                              : `${user.displayName} can sign in again.`,
                          )
                        }
                      >
                        {user.status === "active" ? "Disable" : "Enable"}
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <AddUserDialog
        open={adding}
        onClose={() => setAdding(false)}
        onAdded={(user) => {
          setData([...(data ?? []), user]);
          toast(`${user.displayName} can now sign in.`);
        }}
      />
    </>
  );
}
