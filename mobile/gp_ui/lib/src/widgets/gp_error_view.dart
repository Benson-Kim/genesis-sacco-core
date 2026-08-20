import 'package:flutter/material.dart';
import '../tokens/palette.dart';
import '../tokens/typography.dart';

/// Standard error / empty-state widget used across both apps.
/// Surfaces a sanitized [category] and [correlationId] — never a stack trace
/// (MASTER_PROMPT §1.6).
class GpErrorView extends StatelessWidget {
  const GpErrorView({
    super.key,
    required this.category,
    this.correlationId,
    this.onRetry,
  });

  final String category;
  final String? correlationId;
  final VoidCallback? onRetry;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(32),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(Icons.error_outline_rounded,
                size: 48, color: GpPalette.brick),
            const SizedBox(height: 16),
            Text(category,
                style: GpTypography.titleMedium, textAlign: TextAlign.center),
            if (correlationId != null) ...[
              const SizedBox(height: 8),
              Text(
                'Ref: $correlationId',
                style: GpTypography.labelSmall,
                textAlign: TextAlign.center,
              ),
            ],
            if (onRetry != null) ...[
              const SizedBox(height: 24),
              ElevatedButton.icon(
                onPressed: onRetry,
                icon: const Icon(Icons.refresh_rounded),
                label: const Text('Retry'),
              ),
            ],
          ],
        ),
      ),
    );
  }
}
