import 'package:flutter/material.dart';
import '../tokens/palette.dart';

/// Centered loading indicator using the brand colour.
class GpLoadingView extends StatelessWidget {
  const GpLoadingView({super.key});

  @override
  Widget build(BuildContext context) {
    return const Center(
      child: CircularProgressIndicator(
        valueColor: AlwaysStoppedAnimation<Color>(GpPalette.navy),
      ),
    );
  }
}
