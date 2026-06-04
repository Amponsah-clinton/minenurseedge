-- ============================================================
-- NursesEdge Referral System Schema
-- Run this entire script in your Supabase SQL Editor
-- ============================================================

-- Step 1: Add referral columns to the profiles table
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS referral_code VARCHAR(5);
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS referred_by_code VARCHAR(5);

-- Step 2: Create a unique index on referral_code (NULLs are excluded by default)
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE tablename = 'profiles'
      AND indexname  = 'profiles_referral_code_unique_idx'
  ) THEN
    CREATE UNIQUE INDEX profiles_referral_code_unique_idx
      ON profiles(referral_code)
      WHERE referral_code IS NOT NULL;
  END IF;
END $$;

-- Step 3: Create referral_earnings table
CREATE TABLE IF NOT EXISTS referral_earnings (
    id                 UUID             DEFAULT gen_random_uuid() PRIMARY KEY,
    referrer_id        UUID,
    referrer_name      TEXT             NOT NULL DEFAULT '',
    referrer_email     TEXT             NOT NULL DEFAULT '',
    referred_id        UUID,
    referred_name      TEXT             NOT NULL DEFAULT '',
    referred_email     TEXT             NOT NULL DEFAULT '',
    referral_code      VARCHAR(5)       NOT NULL DEFAULT '',
    subscription_amount NUMERIC(10, 2)  NOT NULL DEFAULT 0,
    earning_amount      NUMERIC(10, 2)  NOT NULL DEFAULT 0,
    earning_percentage  NUMERIC(5, 2)   NOT NULL DEFAULT 10.0,
    status             TEXT             NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'paid')),
    paid_at            TIMESTAMP WITH TIME ZONE,
    paid_by_admin      TEXT,
    created_at         TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Step 4: Indexes for fast look-ups
CREATE INDEX IF NOT EXISTS idx_ref_earnings_referrer  ON referral_earnings(referrer_id);
CREATE INDEX IF NOT EXISTS idx_ref_earnings_referred  ON referral_earnings(referred_id);
CREATE INDEX IF NOT EXISTS idx_ref_earnings_status    ON referral_earnings(status);
CREATE INDEX IF NOT EXISTS idx_ref_earnings_created   ON referral_earnings(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_profiles_ref_code      ON profiles(referral_code);

-- Step 5: Disable RLS for service-role access (or adjust to match your existing policy setup)
ALTER TABLE referral_earnings ENABLE ROW LEVEL SECURITY;

-- Allow service-role (Django backend) full access
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'referral_earnings'
      AND policyname = 'service_role_all_referral_earnings'
  ) THEN
    CREATE POLICY "service_role_all_referral_earnings"
      ON referral_earnings
      FOR ALL
      USING (true)
      WITH CHECK (true);
  END IF;
END $$;

-- ============================================================
-- Done. Verify with:
--   SELECT column_name FROM information_schema.columns
--   WHERE table_name = 'profiles' AND column_name IN ('referral_code','referred_by_code');
--
--   SELECT table_name FROM information_schema.tables
--   WHERE table_name = 'referral_earnings';
-- ============================================================
