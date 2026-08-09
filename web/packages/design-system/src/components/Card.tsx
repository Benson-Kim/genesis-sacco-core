import type { HTMLAttributes } from "react";
import styles from "./Card.module.css";

export interface CardProps extends HTMLAttributes<HTMLDivElement> {
  /** Apply the prototype `.pad` inner padding. */
  padded?: boolean;
}

export function Card({ padded = true, className, ...rest }: CardProps) {
  const classes = [styles.card, padded ? styles.pad : "", className]
    .filter(Boolean)
    .join(" ");
  return <div {...rest} className={classes} />;
}
