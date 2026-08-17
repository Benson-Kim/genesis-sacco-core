import 'package:flutter/material.dart';
import '../tokens/palette.dart';
import '../tokens/typography.dart';

/// A summary card matching the prototype dashboard stat tiles.
class GpStatCard extends StatelessWidget {
  const GpStatCard({
    super.key,
    required this.label,
    required this.value,
    this.trend,
    this.trendUp,
    this.backgroundColor = GpPalette.card,
  });

  final String label;
  final String value;
  final String? trend;
  final bool? trendUp;
  final Color backgroundColor;

  @override
  Widget build(BuildContext context) {
    return Card(
      color: backgroundColor,
      elevation: 0,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(12),
        side: const BorderSide(color: GpPalette.line),
      ),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(label, style: GpTypography.bodyMedium),
            const SizedBox(height: 8),
            Text(value, style: GpTypography.moneyLarge),
            if (trend != null) ...[
              const SizedBox(height: 4),
              Row(
                children: [
                  Icon(
                    trendUp == true
                        ? Icons.arrow_upward_rounded
                        : Icons.arrow_downward_rounded,
                    size: 14,
                    color:
                        trendUp == true ? GpPalette.emerald : GpPalette.orange,
                  ),
                  const SizedBox(width: 2),
                  Text(
                    trend!,
                    style: GpTypography.labelSmall.copyWith(
                      color: trendUp == true
                          ? GpPalette.emerald
                          : GpPalette.orange,
                    ),
                  ),
                ],
              ),
            ],
          ],
        ),
      ),
    );
  }
}
