import pytest
from openpyxl import Workbook

from backend.db.ingest import import_file, read_report
from backend.services.sourcing.analysis import listing_metrics


def test_csv_preamble_bom_and_repeat_safe_overlapping_exports(db, tmp_path):
    content = '\ufeffeBay report\nItem Number,Item Title,Order Number,Quantity,Sale Date,Custom Label\n123,Jeep Wrangler cabin filter,order1,3,2026-09-01,abc\n'
    a = tmp_path / 'a.csv'
    a.write_text(content)
    b = tmp_path / 'b.csv'
    b.write_text(content + '124,Jeep Wrangler oil filter,order2,5,2026-09-02,def\n')
    assert import_file(db, a) == 1
    assert import_file(db, a) == 0
    assert import_file(db, b) == 2
    assert db.execute('SELECT sum(quantity) FROM sales').fetchone()[0] == 8


def test_xlsx_preserves_text_ids_and_parses_date(db, tmp_path):
    from datetime import datetime
    path = tmp_path / 'orders.xlsx'
    book = Workbook()
    book.active.append(['Item Number', 'Item Title', 'Order Number', 'Quantity', 'Sale Date'])
    book.active.append(['001234567890123456', 'Toyota Tacoma floor mat', '001', 7, datetime(2026, 9, 1)])
    book.save(path)
    import_file(db, path)
    assert db.execute('SELECT item_id FROM listings').fetchone()[0] == '001234567890123456'


def test_inventory_quantity_is_never_units_sold(db, tmp_path):
    path = tmp_path / 'inventory.csv'
    path.write_text('Item number,Title,Available quantity,Sold quantity\n1,garage door spring,900,20\n')
    with pytest.raises(ValueError, match='date'):
        import_file(db, path)
    import_file(db, path, snapshot_date='2026-09-01')
    import_file(db, path, snapshot_date='2026-09-08')
    assert db.execute('SELECT count(*) FROM snapshots').fetchone()[0] == 2
    assert listing_metrics(db, '2026-09-08', 7)[0]['total_units_sold'] == 0


@pytest.mark.parametrize('quantity', ['-1', '1.5', '', 'bad'])
def test_bad_rows_fail_whole_file(db, tmp_path, quantity):
    path = tmp_path / 'bad.csv'
    path.write_text('Item Number,Title,Order Number,Quantity,Sale Date\n1,pressure washer nozzle,o1,2,2026-09-01\n2,pressure washer hose,o2,' + quantity + ',2026-09-02\n')
    with pytest.raises(ValueError, match='row 3'):
        import_file(db, path)
    assert db.execute('SELECT count(*) FROM listings').fetchone()[0] == 0


def test_conflicting_order_rolls_back_new_listings(db, tmp_path):
    path = tmp_path / 'orders.csv'
    path.write_text('Item Number,Title,Order Number,Quantity,Sale Date\n1,garage door cable,o1,2,2026-09-01\n')
    import_file(db, path)
    path.write_text('Item Number,Title,Order Number,Quantity,Sale Date\n2,garage door opener,o2,3,2026-09-01\n1,garage door cable,o1,20,2026-09-01\n')
    with pytest.raises(ValueError, match='Conflicting'):
        import_file(db, path)
    assert db.execute('SELECT count(*) FROM listings').fetchone()[0] == 1


def test_custom_headers_and_cancelled_order(db, tmp_path):
    path = tmp_path / 'custom.csv'
    path.write_text('Item Number,Title,Order Number,Units,Sale Date,Order Status\n1,garage door bracket,order1,8,2026-09-01,Cancelled\n')
    import_file(db, path, mapping={'quantity': 'Units'})
    assert db.execute('SELECT quantity FROM sales').fetchone()[0] == 0


def test_missing_sold_counter_is_rejected(tmp_path):
    path = tmp_path / 'inventory.csv'
    path.write_text('Item Number,Title,Available quantity\n1,garage door bracket,8\n')
    with pytest.raises(ValueError, match='sold_quantity'):
        read_report(path)


def test_duplicate_lines_are_rejected_not_double_counted(db, tmp_path):
    path = tmp_path / 'duplicate.csv'
    row = '1,garage door cable,o1,2,2026-09-01\n'
    path.write_text('Item Number,Title,Order Number,Quantity,Sale Date\n' + row + row)
    with pytest.raises(ValueError, match='Duplicate'):
        import_file(db, path)
    assert db.execute('SELECT count(*) FROM sales').fetchone()[0] == 0


def test_distinct_transactions_for_same_order_parent_are_summed(db, tmp_path):
    path = tmp_path / 'variations.csv'
    path.write_text('Item Number,Title,Order Number,Quantity,Sale Date,Transaction ID\n1,garage door cable,o1,2,2026-09-01,t1\n1,garage door cable,o1,2,2026-09-01,t2\n')
    import_file(db, path)
    assert db.execute('SELECT quantity FROM sales').fetchone()[0] == 4


def test_asin_decoded_from_tool_sku():
    from backend.db.ingest import asin_from_sku, product_key
    assert asin_from_sku('v0a1b2c3dQjBURVNUQVNJTg') == 'B0TESTASIN'
    assert product_key({'sku': 'v0a1b2c3dQjBURVNUQVNJTg', 'title': 'x'}) == 'asin:B0TESTASIN'
    assert product_key({'sku': 'vzzzzzzzzQjBURVNUQVNJTg', 'title': 'x'}) == 'sku:vzzzzzzzzQjBURVNUQVNJTg'
    assert product_key({'sku': 'v8abf2dcfaGVsbG8gd29ybGQ', 'title': 'x'}).startswith('sku:')
    assert product_key({'sku': '', 'title': 'Garage Door'}) == 'title:garage door'


def test_asin_upgrades_earlier_proxy_and_existing_rows_migrate(db, tmp_path):
    from backend.db.connection import connect
    from backend.db.ingest import save_rows
    with db:
        save_rows(db, [{'item_id': '1', 'title': 'garage door cable', 'sku': '100000000001', 'order_id': 'o',
                        'sold_date': '2026-09-01', 'quantity': 1}], 'orders')
        save_rows(db, [{'item_id': '1', 'title': 'garage door cable', 'sku': 'v0a1b2c3dQjBURVNUQVNJTg',
                        'sold_quantity': 1, 'observed_date': '2026-09-02', 'status': 'active',
                        'start_date': '2026-08-01', 'end_date': None}], 'listings')
    row = db.execute('SELECT product_key, start_date, first_seen FROM listings').fetchone()
    assert tuple(row) == ('asin:B0TESTASIN', '2026-08-01', '2026-09-01')
    path = tmp_path / 'old.sqlite'
    import sqlite3
    old = sqlite3.connect(path)
    old.execute('CREATE TABLE listings (item_id TEXT PRIMARY KEY, title TEXT NOT NULL, product_key TEXT NOT NULL, first_seen TEXT NOT NULL)')
    old.execute("INSERT INTO listings VALUES ('1','t','sku:v0a1b2c3dQjBURVNUQVNJTg','2026-09-01')")
    old.commit()
    old.close()
    upgraded = connect(str(path))
    assert tuple(upgraded.execute('SELECT product_key, start_date FROM listings').fetchone()) == ('asin:B0TESTASIN', None)
    upgraded.close()
