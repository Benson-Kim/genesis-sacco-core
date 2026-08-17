import { Card, Stat } from "@genesis/design-system";
import styles from "./page.module.css";

/**
 * Dashboard shell — real portfolio figures arrive with P15 (fed by the P10
 * portfolio endpoints). Scaffold shows the frame only, no fake numbers.
 */
export default function DashboardPage() {
  return (
    <div className={styles.grid}>
      <Card>
        <Stat label="Members" value="—" />
      </Card>
      <Card>
        <Stat label="Loan book (KES)" value="—" />
      </Card>
      <Card>
        <Stat label="Deposits (KES)" value="—" />
      </Card>
      <Card>
        <Stat label="PAR-30" value="—" />
      </Card>
      <Card className={styles.wide}>
        <h2 className={styles.title}>Portfolio overview</h2>
        <p className={styles.body}>
          Dashboard widgets are wired to live data in P15 (Web admin features), module
          by module, on top of this scaffold.
        </p>
      </Card>
    </div>
  );
}
