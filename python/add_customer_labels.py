import csv
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

def add_customer_labels(contacts_file, customers_file, output_file):
    """Add 'customer' label to contacts that match customer phone numbers."""
    print(f"Processing files...")
    
    # Read customers file and extract phone numbers
    print(f"Reading customer list from {customers_file}...")
    with open(customers_file, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        customer_rows = list(reader)
    
    # Find phone columns in customer file
    if not customer_rows:
        raise ValueError("Musteri CSV dosyasi bos.")
    customer_header = customer_rows[0]
    customer_phone_columns = [i for i, col in enumerate(customer_header) if 'Phone' in col and 'Value' in col]
    
    # Extract all customer phone numbers
    customer_phones = set()
    for row in customer_rows[1:]:
        for phone_col in customer_phone_columns:
            if phone_col < len(row) and row[phone_col] and row[phone_col].strip():
                customer_phones.add(row[phone_col].strip())
    
    print(f"Found {len(customer_phones)} unique customer phone numbers")
    
    # Read contacts file
    print(f"Reading contacts from {contacts_file}...")
    with open(contacts_file, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        contact_rows = list(reader)
    
    # Find columns in contacts file
    if not contact_rows:
        raise ValueError("Kisi CSV dosyasi bos.")
    contact_header = contact_rows[0]
    phone_columns = [i for i, col in enumerate(contact_header) if 'Phone' in col and 'Value' in col]
    labels_column = None
    for i, col in enumerate(contact_header):
        if 'Labels' in col:
            labels_column = i
            break
    
    print(f"Labels column index: {labels_column}")
    
    # Update contacts with customer label
    updated_count = 0
    for row in contact_rows[1:]:
        # Get the first phone number
        phone = None
        for phone_col in phone_columns:
            if phone_col < len(row) and row[phone_col] and row[phone_col].strip():
                phone = row[phone_col].strip()
                break
        
        if phone and phone in customer_phones:
            # Update labels column
            if labels_column is not None and labels_column < len(row):
                current_labels = row[labels_column] if row[labels_column] else ''
                # Add customer label if not already present
                if 'customer' not in current_labels.lower():
                    if current_labels:
                        row[labels_column] = current_labels + ' ::: customer'
                    else:
                        row[labels_column] = 'customer'
                    updated_count += 1
    
    print(f"Updated {updated_count} contacts with customer label")
    
    # Save to output file
    with open(output_file, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerows(contact_rows)
    
    print(f"Saved updated contacts to {output_file}")

if __name__ == "__main__":
    add_customer_labels(
        BASE / "ma_contacts2026_cleaned.csv",
        BASE / "Liste" / "WhatsApp_MA_Musteriler2026.csv",
        BASE / "ma_contacts2026_cleaned.csv",
    )
    print("\nDone! Customer labels have been added.")
